import asyncio
import os
import json
import time
import threading
import io
import sys
import traceback  # 添加traceback模块导入
import xml.etree.ElementTree as ET  # 在顶部添加ET导入
import aiohttp  # 添加aiohttp模块导入
import re  # 添加re模块导入
from typing import Dict, Any, Optional, List, Tuple
import urllib.parse  # 添加urllib.parse模块导入
import requests
from bridge.context import Context, ContextType  # 确保导入Context类
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.chat_message import ChatMessage
from channel.wx849.wx849_message import WX849Message  # 改为从wx849_message导入WX849Message
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.time_check import time_checker
from common.utils import remove_markdown_symbol
from config import conf, get_appdata_dir
from voice.audio_convert import split_audio # Added for voice splitting
from common.tmp_dir import TmpDir # Added for temporary file management
# 新增HTTP服务器相关导入
from aiohttp import web
import uuid
import re
import base64
import subprocess
import math
from pydub import AudioSegment # Added for audio duration
from io import BytesIO # Added for pydub if it operates on BytesIO
import functools

# Attempt to import pysilk
try:
    import pysilk
    PYSLIK_AVAILABLE = True
    logger.info("[WX849] pysilk library loaded successfully.")
except ImportError:
    PYSLIK_AVAILABLE = False
    logger.warning("[WX849] pysilk library not found. Voice message SILK encoding will be unavailable.")

# 增大日志行长度限制，以便完整显示XML内容
try:
    import logging
    # 尝试设置日志格式化器的最大长度限制
    for handler in logging.getLogger().handlers:
        if hasattr(handler, 'formatter'):
            handler.formatter._fmt = handler.formatter._fmt.replace('%(message)s', '%(message).10000s')
    logger.info("[WX849] 已增大日志输出长度限制")
except Exception as e:
    logger.warning(f"[WX849] 设置日志长度限制失败: {e}")

# 添加 wx849 目录到 sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
# 修改路径查找逻辑，确保能找到正确的 lib/wx849 目录
# 尝试多种可能的路径
possible_lib_dirs = [
    # 尝试相对项目根目录路径
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(current_dir))), "lib", "wx849"),
    # 尝试当前目录的上一级
    os.path.join(os.path.dirname(os.path.dirname(current_dir)), "lib", "wx849"),
    # 尝试当前目录的上上级
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))), "lib", "wx849"),
    # 尝试绝对路径（Windows兼容写法）
    os.path.join(os.path.abspath(os.sep), "root", "dow-849", "lib", "wx849")
]

# 尝试所有可能的路径
lib_dir = None
for possible_dir in possible_lib_dirs:
    if os.path.exists(possible_dir):
        lib_dir = possible_dir
        break

# 打印路径信息以便调试
logger.info(f"WechatAPI 模块搜索路径尝试列表: {possible_lib_dirs}")
logger.info(f"最终选择的WechatAPI模块路径: {lib_dir}")

if lib_dir and os.path.exists(lib_dir):
    if lib_dir not in sys.path:
        sys.path.append(lib_dir)
    # 直接添加 WechatAPI 目录到路径
    wechat_api_dir = os.path.join(lib_dir, "WechatAPI")
    if os.path.exists(wechat_api_dir) and wechat_api_dir not in sys.path:
        sys.path.append(wechat_api_dir)
    logger.info(f"已添加 WechatAPI 模块路径: {lib_dir}")
    logger.info(f"Python 搜索路径: {sys.path}")
else:
    logger.error(f"WechatAPI 模块路径不存在，尝试的所有路径均不可用")

# 导入 WechatAPI 客户端
try:
    # 使用不同的导入方式尝试
    try:
        # 尝试方式1：直接导入
        import WechatAPI
        from WechatAPI import WechatAPIClient
        logger.info("成功导入 WechatAPI 模块（方式1）")
    except ImportError:
        try:
            # 尝试方式2：从相对路径导入
            sys.path.append(os.path.dirname(lib_dir))
            from wx849.WechatAPI import WechatAPIClient
            import wx849.WechatAPI as WechatAPI
            logger.info("成功导入 WechatAPI 模块（方式2）")
        except ImportError:
            # 尝试方式3：Windows特殊处理
            if os.name == 'nt':  # Windows系统
                # 列出所有可能的库路径
                for path in sys.path:
                    if 'wx849' in path:
                        logger.info(f"在路径中查找wx849: {path}")
                        if os.path.exists(path):
                            subdirs = os.listdir(path)
                            logger.info(f"目录 {path} 下的内容: {subdirs}")
                
                # 尝试直接将wx849目录加入sys.path
                parent_dir = os.path.dirname(current_dir) # channel目录
                project_dir = os.path.dirname(parent_dir) # 项目根目录
                wx849_lib_dir = os.path.join(project_dir, "lib", "wx849")
                
                if os.path.exists(wx849_lib_dir):
                    if wx849_lib_dir not in sys.path:
                        sys.path.append(wx849_lib_dir)
                    
                    # 尝试导入
                    import WechatAPI
                    from WechatAPI import WechatAPIClient
                    logger.info("成功导入 WechatAPI 模块（Windows特殊处理）")
                else:
                    raise ImportError(f"在Windows系统上找不到wx849库: {wx849_lib_dir}")
            else:
                raise
    
    # 设置 WechatAPI 的 loguru 日志级别（关键修改）
    try:
        from loguru import logger as api_logger
        import logging
        
        # 移除所有现有处理器
        api_logger.remove()
        
        # 获取配置的日志级别，默认为 ERROR 以减少输出
        log_level = conf().get("log_level", "ERROR")
        
        # 添加新的处理器，仅输出 ERROR 级别以上的日志
        api_logger.add(sys.stderr, level=log_level)
        logger.info(f"已设置 WechatAPI 日志级别为: {log_level}")
    except Exception as e:
        logger.error(f"设置 WechatAPI 日志级别时出错: {e}")
except Exception as e:
    logger.error(f"导入 WechatAPI 模块失败: {e}")
    # 打印更详细的调试信息
    logger.error(f"当前Python路径: {sys.path}")
    
    # 检查目录内容
    if lib_dir and os.path.exists(lib_dir):
        logger.info(f"lib_dir 目录内容: {os.listdir(lib_dir)}")
        wechat_api_dir = os.path.join(lib_dir, "WechatAPI")
        if os.path.exists(wechat_api_dir):
            logger.info(f"WechatAPI 目录内容: {os.listdir(wechat_api_dir)}")
    
    # 打印堆栈信息
    import traceback
    logger.error(f"详细错误信息: {traceback.format_exc()}")
    
    raise ImportError(f"无法导入 WechatAPI 模块，请确保 wx849 目录已正确配置: {e}")

# 添加 ContextType.PAT 类型（如果不存在）
if not hasattr(ContextType, 'PAT'):
    setattr(ContextType, 'PAT', 'PAT')
if not hasattr(ContextType, 'QUOTE'):
    setattr(ContextType, 'QUOTE', 'QUOTE')
# 添加 ContextType.UNKNOWN 类型（如果不存在）
if not hasattr(ContextType, 'UNKNOWN'):
    setattr(ContextType, 'UNKNOWN', 'UNKNOWN')
# 添加 ContextType.XML 类型（如果不存在）
if not hasattr(ContextType, 'XML'):
    setattr(ContextType, 'XML', 'XML')
    logger.info("[WX849] 已添加 ContextType.XML 类型")
# 添加其他可能使用的ContextType类型
if not hasattr(ContextType, 'LINK'):
    setattr(ContextType, 'LINK', 'LINK')
    logger.info("[WX849] 已添加 ContextType.LINK 类型")
if not hasattr(ContextType, 'FILE'):
    setattr(ContextType, 'FILE', 'FILE')
    logger.info("[WX849] 已添加 ContextType.FILE 类型")
if not hasattr(ContextType, 'MINIAPP'):
    setattr(ContextType, 'MINIAPP', 'MINIAPP')
    logger.info("[WX849] 已添加 ContextType.MINIAPP 类型")
if not hasattr(ContextType, 'SYSTEM'):
    setattr(ContextType, 'SYSTEM', 'SYSTEM')
    logger.info("[WX849] 已添加 ContextType.SYSTEM 类型")
if not hasattr(ContextType, 'VIDEO'):
    setattr(ContextType, 'VIDEO', 'VIDEO')
    logger.info("[WX849] 已添加 ContextType.VIDEO 类型")

# 导入cv2（OpenCV）用于处理视频
try:
    import cv2
    logger.info("[WX849] 成功导入OpenCV(cv2)模块")
except ImportError:
    logger.warning("[WX849] 未安装OpenCV(cv2)模块，视频处理功能将受限")
    cv2 = None

def _find_ffmpeg_path():
    """Finds the ffmpeg executable path."""
    ffmpeg_cmd = "ffmpeg" # Default command
    if os.name == 'nt': # Windows
        possible_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            * [os.path.join(p, "ffmpeg.exe") for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        ]
        for path in possible_paths:
            if os.path.exists(path):
                ffmpeg_cmd = path
                logger.debug(f"[WX849] Found ffmpeg at: {ffmpeg_cmd}")
                return ffmpeg_cmd
        logger.warning("[WX849] ffmpeg not found in common Windows paths or PATH, will try system PATH with 'ffmpeg'.")
        return "ffmpeg"
    else: # Linux/macOS
        import shutil
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            logger.debug(f"[WX849] Found ffmpeg at: {ffmpeg_path}")
            return ffmpeg_path
        else:
            logger.warning("[WX849] ffmpeg not found using shutil.which. Will try system PATH with 'ffmpeg'.")
            return "ffmpeg"

def _check(func):
    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def wrapper(self, cmsg: ChatMessage):
            msgId = cmsg.msg_id
            if not msgId:
                msgId = f"msg_{int(time.time())}_{hash(str(cmsg.msg))}"
                logger.debug(f"[WX849] _check: 为空消息ID生成唯一ID: {msgId}")
            
            if msgId in self.received_msgs:
                logger.debug(f"[WX849] 消息 {msgId} 已处理过，忽略")
                return
            
            self.received_msgs[msgId] = True
            
            create_time = cmsg.create_time
            current_time = int(time.time())
            timeout = 60
            if int(create_time) < current_time - timeout:
                logger.debug(f"[WX849] 历史消息 {msgId} 已跳过，时间差: {current_time - int(create_time)}秒")
                return
            return await func(self, cmsg)
        return wrapper
    else:
        @functools.wraps(func)
        def wrapper(self, cmsg: ChatMessage):
            msgId = cmsg.msg_id
            if not msgId:
                msgId = f"msg_{int(time.time())}_{hash(str(cmsg.msg))}"
                logger.debug(f"[WX849] _check: 为空消息ID生成唯一ID: {msgId}")

            if msgId in self.received_msgs:
                logger.debug(f"[WX849] 消息 {msgId} 已处理过，忽略")
                return

            self.received_msgs[msgId] = True

            create_time = cmsg.create_time
            current_time = int(time.time())
            timeout = 60
            if int(create_time) < current_time - timeout:
                logger.debug(f"[WX849] 历史消息 {msgId} 已跳过，时间差: {current_time - int(create_time)}秒")
                return
            return func(self, cmsg)
        return wrapper

@singleton
class WX849Channel(ChatChannel):
    """
    wx849 channel - 独立通道实现
    """
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        self.received_msgs = ExpiredDict(conf().get("expires_in_seconds", 3600))
        self.recent_image_msgs = ExpiredDict(conf().get("image_expires_in_seconds", 7200)) # Added initialization
        self.bot = None
        self.user_id = None
        self.name = None
        self.wxid = None
        self.is_running = False
        self.is_logged_in = False
        self.group_name_cache = {}

    async def _initialize_bot(self):
        """初始化 bot"""
        logger.info("[WX849] 正在初始化 bot...")
        
        # 读取协议版本设置
        protocol_version = conf().get("wx849_protocol_version", "849")
        logger.info(f"使用协议版本: {protocol_version}")
        
        api_host = conf().get("wx849_api_host", "127.0.0.1")
        api_port = conf().get("wx849_api_port", 9000)
        
        # 设置API路径前缀，根据协议版本区分
        if protocol_version == "855" or protocol_version == "ipad":
            api_path_prefix = "/api"
            logger.info(f"使用API路径前缀: {api_path_prefix} (适用于{protocol_version}协议)")
        else:
            api_path_prefix = "/VXAPI"
            logger.info(f"使用API路径前缀: {api_path_prefix} (适用于849协议)")
        
        # 初始化WechatAPI客户端
        try:
            # 根据协议版本选择不同的客户端
            if protocol_version == "855":
                try:
                    from WechatAPI.Client2 import WechatAPIClient as WechatAPIClient2
                    self.bot = WechatAPIClient2(api_host, api_port)
                    logger.info("成功加载855协议客户端")
                except Exception as e:
                    logger.error(f"加载855协议客户端失败: {e}")
                    logger.warning("回退使用默认客户端")
                    self.bot = WechatAPI.WechatAPIClient(api_host, api_port)
            elif protocol_version == "ipad":
                try:
                    from WechatAPI.Client3 import WechatAPIClient as WechatAPIClient3
                    self.bot = WechatAPIClient3(api_host, api_port)
                    logger.info("成功加载iPad协议客户端")
                except Exception as e:
                    logger.error(f"加载iPad协议客户端失败: {e}")
                    logger.warning("回退使用默认客户端")
                    self.bot = WechatAPI.WechatAPIClient(api_host, api_port)
            else:
                self.bot = WechatAPI.WechatAPIClient(api_host, api_port)
                logger.info("使用849协议客户端")
            
            # 设置API路径前缀
            if hasattr(self.bot, "set_api_path_prefix"):
                self.bot.set_api_path_prefix(api_path_prefix)
                
            # 设置bot的ignore_protection属性为True，强制忽略所有风控保护
            if hasattr(self.bot, "ignore_protection"):
                self.bot.ignore_protection = True
                logger.info("[WX849] 已设置忽略风控保护")
        except Exception as e:
            logger.error(f"[WX849] 初始化WechatAPI客户端失败: {e}")
            return False
        
        # 等待 WechatAPI 服务启动
        service_ok = await self._check_api_service(api_host, api_port, api_path_prefix)
        if not service_ok:
            logger.error("[WX849] WechatAPI 服务连接失败")
            return False
        
        # 检查并读取保存的设备信息和登录信息
        device_info_path = os.path.join(get_appdata_dir(), "wx849_device_info.json")
        
        # 默认设备信息
        saved_wxid = ""
        saved_device_id = ""
        saved_device_name = "DoW微信机器人"
        
        # 读取已保存的设备信息
        if os.path.exists(device_info_path):
            try:
                with open(device_info_path, "r", encoding="utf-8") as f:
                    device_info = json.load(f)
                    saved_wxid = device_info.get("wxid", "")
                    saved_device_id = device_info.get("device_id", "")
                    saved_device_name = device_info.get("device_name", "DoW微信机器人")
                    
                    logger.info(f"[WX849] 已读取保存的设备信息: wxid={saved_wxid}, device_id={saved_device_id}")
            except Exception as e:
                logger.error(f"[WX849] 读取设备信息文件失败: {e}")
        
        # 从配置中读取是否启用自动登录
        # 原来的代码会尝试读取wx849_auto_login配置项，现在直接使用True
        # auto_login_enabled = conf().get("wx849_auto_login", True)
        auto_login_enabled = True  # 默认启用自动登录
        
        # 尝试自动登录
        if auto_login_enabled and saved_wxid:
            auto_login_success = await self._auto_login(saved_wxid, saved_device_id, saved_device_name)
            if auto_login_success:
                return True
        
        # 自动登录失败或未启用，进行扫码登录
        logger.info("[WX849] 自动登录失败或未启用，使用扫码登录")
        
        # 生成device_name和device_id
        device_name = saved_device_name or "DoW微信机器人"
        device_id = saved_device_id or ""
        
        if hasattr(self.bot, "create_device_name") and not device_name:
            device_name = self.bot.create_device_name()
            
        if hasattr(self.bot, "create_device_id") and not device_id:
            device_id = self.bot.create_device_id()
        
        # 获取登录二维码
        logger.info("[WX849] 开始获取登录二维码")
        try:
            # 修改调用方式，使用小写参数
            uuid, url = await self.bot.get_qr_code(device_id=device_id, device_name=device_name, print_qr=True)
            logger.info(f"[WX849] 获取到登录uuid: {uuid}")
            logger.info(f"[WX849] 获取到登录二维码: {url}")
        except Exception as e:
            logger.error(f"[WX849] 获取登录二维码失败: {e}")
            return False
        
        # 等待扫码并登录
        login_success, new_wxid = await self._wait_for_qr_login(uuid, device_id, device_name, device_info_path)
        return login_success

    async def _check_api_service(self, api_host, api_port, api_path_prefix):
        """检查API服务是否可用"""
        logger.info(f"尝试连接到 WechatAPI 服务 (地址: {api_host}:{api_port}{api_path_prefix})")
        
        time_out = 30
        is_connected = False
        
        while not is_connected and time_out > 0:
            try:
                # 尝试使用bot对象的is_running方法
                if hasattr(self.bot, "is_running") and await self.bot.is_running():
                    is_connected = True
                    logger.info("[WX849] API服务已通过is_running方法确认可用")
                    break
                
                # 如果bot对象的方法失败，尝试直接发送HTTP请求检查服务是否可用
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    try:
                        # 尝试访问登录接口，确保URL格式正确
                        url = f"http://{api_host}:{api_port}{api_path_prefix}/Login/GetQR"
                        logger.debug(f"尝试连接: {url}")
                        async with session.get(url, timeout=5) as response:
                            if response.status in [200, 401, 403, 404]:  # 任何HTTP响应都表示服务在运行
                                is_connected = True
                                logger.info("[WX849] 通过HTTP请求确认服务可用")
                                break
                    except Exception as e:
                        logger.debug(f"API路径请求失败: {e}")
                        
                        # 如果特定路径失败，尝试访问根路径
                        url = f"http://{api_host}:{api_port}/"
                        logger.debug(f"尝试连接根路径: {url}")
                        try:
                            async with session.get(url, timeout=5) as response:
                                if response.status in [200, 401, 403, 404]:
                                    is_connected = True
                                    logger.info("[WX849] 通过根路径确认服务可用")
                                    break
                        except Exception as e2:
                            logger.debug(f"根路径请求也失败: {e2}")
            except Exception as e:
                logger.debug(f"连接尝试失败: {e}")
            
            logger.info("等待 WechatAPI 启动中")
            await asyncio.sleep(2)
            time_out -= 2
        
        return is_connected

    async def _wait_for_qr_login(self, uuid, device_id, device_name, device_info_path):
        """等待扫码登录完成"""
        login_timeout = 120
        
        while login_timeout > 0:
            try:
                # 检查登录状态 - 修改参数为小写
                login_success, login_result = await self.bot.check_login_uuid(uuid, device_id=device_id)
                
                if login_success:
                    logger.info("[WX849] 扫码登录成功，已获取登录信息")
                    
                    # 提取微信ID和昵称
                    new_wxid = ""
                    new_name = ""
                    
                    # 提取不同协议版本返回的用户信息
                    if isinstance(login_result, dict):
                        if "acctSectResp" in login_result:
                            acct_resp = login_result["acctSectResp"]
                            new_wxid = acct_resp.get("userName", "")
                            new_name = acct_resp.get("nickName", "")
                        elif "userName" in login_result:
                            new_wxid = login_result["userName"]
                            new_name = login_result.get("nickName", "")
                    
                    if not new_wxid:
                        logger.error("[WX849] 无法从登录结果中获取微信ID")
                        return False, ""
                    
                    # 保存登录和设备信息到本地文件
                    try:
                        device_info = {
                            "wxid": new_wxid,
                            "device_id": device_id,
                            "device_name": device_name
                        }
                        
                        os.makedirs(os.path.dirname(device_info_path), exist_ok=True)
                        with open(device_info_path, "w", encoding="utf-8") as f:
                            json.dump(device_info, f, indent=2)
                        logger.info(f"[WX849] 已保存登录信息到: {device_info_path}")
                    except Exception as e:
                        logger.error(f"[WX849] 保存登录信息失败: {e}")
                    
                    # 设置登录状态
                    self.wxid = new_wxid
                    self.user_id = new_wxid
                    self.name = new_name or new_wxid
                    self.is_logged_in = True
                    
                    # 同步设置bot的wxid属性，确保消息获取不会失败
                    if hasattr(self.bot, 'wxid'):
                        self.bot.wxid = new_wxid
                        logger.info(f"[WX849] 已同步设置bot.wxid = {new_wxid}")
                    else:
                        logger.error(f"[WX849] bot对象没有wxid属性，可能导致消息获取失败")
                    
                    logger.info(f"[WX849] 登录信息: user_id={self.user_id}, nickname={self.name}")
                    
                    # 如果没有获取到名称，尝试获取个人资料
                    if not new_name:
                        threading.Thread(target=lambda: asyncio.run(self._get_user_profile())).start()
                    
                    return True, new_wxid
            except Exception as e:
                logger.error(f"[WX849] 检查扫码登录状态出错: {e}")
            
            # 等待2秒后再次检查
            await asyncio.sleep(2)
            login_timeout -= 2
            logger.info(f"[WX849] 等待扫码登录完成，剩余 {login_timeout} 秒...")
        
        logger.error("[WX849] 扫码登录超时")
        return False, ""

    async def _check_login_status(self, wxid):
        """检查是否已经登录"""
        try:
            logger.info(f"[WX849] 正在检查用户 {wxid} 的登录状态")
            
            # 使用心跳接口检查登录状态
            params = {
                "wxid": wxid,  # 参数名应该为小写的wxid
                "Wxid": wxid   # 同时提供大写参数，增加兼容性
            }
            
            logger.debug(f"[WX849] 心跳接口参数: {params}")
            
            # 调用心跳接口
            response = await self._call_api("/Login/HeartBeat", params)
            
            # 打印响应内容以便调试
            logger.debug(f"[WX849] 心跳接口响应: {response}")
            
            if response and isinstance(response, dict) and response.get("Success", False):
                logger.info(f"[WX849] 心跳检测成功，wxid={wxid}处于登录状态")
                return True
            
            # 详细记录失败原因
            if response and isinstance(response, dict):
                error_msg = response.get("Message", "未知错误")
                error_code = response.get("Code", 0)
                logger.warning(f"[WX849] 心跳检测失败，错误码: {error_code}, 错误信息: {error_msg}")
            else:
                logger.warning(f"[WX849] 心跳检测失败，响应无效: {response}")
            
            logger.warning(f"[WX849] 心跳检测失败，wxid={wxid}不在登录状态")
            return False
        except Exception as e:
            logger.error(f"[WX849] 检查登录状态失败: {e}")
            import traceback
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return False

    async def _get_cached_info(self, wxid):
        """获取登录缓存信息"""
        try:
            logger.info(f"[WX849] 正在获取用户 {wxid} 的登录缓存信息")
            
            params = {
                "wxid": wxid,  # 小写参数名
                "Wxid": wxid   # 同时提供大写参数，增加兼容性
            }
            
            logger.debug(f"[WX849] 缓存信息接口参数: {params}")
            
            # 调用获取缓存信息接口 - 修正接口名
            response = await self._call_api("/Login/GetCacheInfo", params)
            
            # 打印响应内容以便调试
            logger.debug(f"[WX849] 缓存信息接口响应: {response}")
            
            if response and isinstance(response, dict) and response.get("Success", False):
                logger.info(f"[WX849] 成功获取登录缓存信息: wxid={wxid}")
                return response.get("Data", {})
            
            # 详细记录失败原因
            if response and isinstance(response, dict):
                error_msg = response.get("Message", "未知错误")
                error_code = response.get("Code", 0)
                logger.warning(f"[WX849] 获取缓存信息失败，错误码: {error_code}, 错误信息: {error_msg}")
            else:
                logger.warning(f"[WX849] 获取缓存信息失败，响应无效: {response}")
            
            logger.warning(f"[WX849] 获取登录缓存信息失败: wxid={wxid}")
            return None
        except Exception as e:
            logger.error(f"[WX849] 获取登录缓存信息失败: {e}")
            import traceback
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None

    async def _twice_login(self, wxid, device_id=None):
        """尝试二次登录"""
        try:
            logger.info(f"[WX849] 尝试二次登录: wxid={wxid}, device_id={device_id}")
            
            params = {
                "wxid": wxid,  # 小写参数名
                "Wxid": wxid,  # 同时提供大写参数，增加兼容性
                "OSModel": device_id or "iPad"
            }
            
            logger.debug(f"[WX849] 二次登录参数: {params}")
            
            # 调用二次登录接口
            response = await self._call_api("/Login/TwiceAutoAuth", params)
            
            # 打印响应内容以便调试
            logger.debug(f"[WX849] 二次登录响应: {response}")
            
            if response and isinstance(response, dict) and response.get("Success", False):
                logger.info(f"[WX849] 二次登录成功: wxid={wxid}")
                return True
            
            # 详细记录失败原因
            if response and isinstance(response, dict):
                error_msg = response.get("Message", "未知错误")
                error_code = response.get("Code", 0)
                logger.warning(f"[WX849] 二次登录失败，错误码: {error_code}, 错误信息: {error_msg}")
            else:
                logger.warning(f"[WX849] 二次登录失败，响应无效: {response}")
            
            logger.warning(f"[WX849] 二次登录失败: wxid={wxid}, response={response}")
            return False
        except Exception as e:
            logger.error(f"[WX849] 二次登录失败: {e}")
            import traceback
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return False

    async def _awaken_login(self, wxid, device_name="iPad"):
        """尝试唤醒登录"""
        try:
            logger.info(f"[WX849] 尝试唤醒登录: wxid={wxid}, device_name={device_name}")
            
            params = {
                "wxid": wxid,  # 参数名改为小写
                "OS": device_name,
                "Proxy": {
                    "ProxyIp": "",
                    "ProxyPassword": "",
                    "ProxyUser": ""
                },
                "Url": ""
            }
            
            logger.debug(f"[WX849] 唤醒登录参数: {params}")
            
            # 调用唤醒登录接口
            response = await self._call_api("/Login/Awaken", params)
            
            # 打印响应内容以便调试
            logger.debug(f"[WX849] 唤醒登录响应: {response}")
            
            if response and isinstance(response, dict) and response.get("Success", False):
                # 获取UUID
                data = response.get("Data", {})
                qr_response = data.get("QrCodeResponse", {}) if data else {}
                uuid = qr_response.get("Uuid", "") if qr_response else ""
                
                if uuid:
                    logger.info(f"[WX849] 唤醒登录成功，获取到UUID: {uuid}")
                    return uuid
            
            # 详细记录失败原因
            if response and isinstance(response, dict):
                error_msg = response.get("Message", "未知错误")
                error_code = response.get("Code", 0)
                logger.warning(f"[WX849] 唤醒登录失败，错误码: {error_code}, 错误信息: {error_msg}")
            else:
                logger.warning(f"[WX849] 唤醒登录失败，响应无效: {response}")
            
            logger.warning(f"[WX849] 唤醒登录失败，未获取到有效UUID: {response}")
            return None
        except Exception as e:
            logger.error(f"[WX849] 唤醒登录失败: {e}")
            import traceback
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None

    async def _auto_login(self, saved_wxid, saved_device_id, saved_device_name):
        """自动登录流程"""
        if not saved_wxid:
            logger.info("[WX849] 无保存的微信ID，无法执行自动登录")
            return False
        
        logger.info(f"[WX849] 开始自动登录流程: wxid={saved_wxid}")
        
        # 移除一开始的一致性检查，这会导致服务器未登录时直接失败
        # is_consistent = await self._check_api_login_consistency(saved_wxid)
        # if not is_consistent:
        #     logger.warning(f"[WX849] API服务器登录用户与本地保存不一致，重新登录")
        #     return False
        
        # 直接进行心跳检测等自动登录步骤
        
        # 1. 首先检查登录状态 - 通过心跳接口
        logger.info(f"[WX849] 第1步: 检查心跳状态")
        heart_beat_ok = await self._check_login_status(saved_wxid)
        if heart_beat_ok:
            logger.info(f"[WX849] 心跳检测成功，wxid={saved_wxid}已在线")
            
            # 先设置bot的wxid，再调用_set_logged_in_state方法
            if hasattr(self.bot, 'wxid'):
                self.bot.wxid = saved_wxid
                logger.info(f"[WX849] 已同步设置bot.wxid = {saved_wxid}")
            else:
                logger.error(f"[WX849] bot对象没有wxid属性，可能导致消息获取失败")
                
            self._set_logged_in_state(saved_wxid)
            return True
        
        logger.info(f"[WX849] 心跳检测失败，继续尝试其他自动登录方式")
        
        # 2. 如果心跳失败，获取登录缓存信息
        logger.info(f"[WX849] 第2步: 获取登录缓存信息")
        cache_info = await self._get_cached_info(saved_wxid)
        if not cache_info:
            logger.warning(f"[WX849] 无法获取登录缓存信息，自动登录失败")
            return False
        
        logger.info(f"[WX849] 成功获取缓存信息，继续自动登录")
        
        # 3. 尝试二次登录
        logger.info(f"[WX849] 第3步: 尝试二次登录")
        twice_login_ok = await self._twice_login(saved_wxid, saved_device_id)
        if twice_login_ok:
            logger.info(f"[WX849] 二次登录成功: {saved_wxid}")
            
            # 先设置bot的wxid，再调用_set_logged_in_state方法
            if hasattr(self.bot, 'wxid'):
                self.bot.wxid = saved_wxid
                logger.info(f"[WX849] 已同步设置bot.wxid = {saved_wxid}")
            else:
                logger.error(f"[WX849] bot对象没有wxid属性，可能导致消息获取失败")
                
            self._set_logged_in_state(saved_wxid)
            return True
        
        logger.info(f"[WX849] 二次登录失败，尝试唤醒登录")
        
        # 4. 如果二次登录失败，尝试唤醒登录
        logger.info(f"[WX849] 第4步: 尝试唤醒登录")
        uuid = await self._awaken_login(saved_wxid, saved_device_name or "iPad")
        if not uuid:
            logger.warning(f"[WX849] 唤醒登录失败，自动登录流程终止")
            return False
        
        logger.info(f"[WX849] 唤醒登录成功，获取到UUID: {uuid}")
        
        # 5. 等待唤醒登录确认
        logger.info(f"[WX849] 第5步: 等待唤醒登录确认")
        login_result = await self._wait_for_login_confirmation(uuid, saved_device_id)
        if login_result:
            logger.info(f"[WX849] 唤醒登录确认成功: {saved_wxid}")
            
            # 先设置bot的wxid，再调用_set_logged_in_state方法
            if hasattr(self.bot, 'wxid'):
                self.bot.wxid = saved_wxid
                logger.info(f"[WX849] 已同步设置bot.wxid = {saved_wxid}")
            else:
                logger.error(f"[WX849] bot对象没有wxid属性，可能导致消息获取失败")
                
            self._set_logged_in_state(saved_wxid)
            return True
        
        logger.warning(f"[WX849] 唤醒登录确认失败，自动登录流程失败")
        return False

    async def _wait_for_login_confirmation(self, uuid, device_id):
        """等待唤醒登录确认"""
        timeout = 60  # 60秒超时
        logger.info(f"[WX849] 等待唤醒登录确认，UUID: {uuid}, 设备ID: {device_id}, 超时时间: {timeout}秒")
        
        while timeout > 0:
            try:
                logger.info(f"[WX849] 等待唤醒登录确认，剩余 {timeout} 秒...")
                
                # 检查登录状态 - 确保使用小写参数
                logger.debug(f"[WX849] 检查登录UUID状态: {uuid}")
                login_success, login_result = await self.bot.check_login_uuid(uuid, device_id=device_id)
                
                # 记录结果详情
                logger.debug(f"[WX849] 检查登录UUID结果: success={login_success}, result={login_result}")
                
                if login_success:
                    logger.info("[WX849] 唤醒登录确认成功")
                    return True
                else:
                    # 如果未成功，记录更详细的状态信息
                    if isinstance(login_result, dict):
                        status = login_result.get("Status", "未知")
                        msg = login_result.get("Message", "")
                        logger.debug(f"[WX849] 登录状态: {status}, 消息: {msg}")
            except Exception as e:
                logger.error(f"[WX849] 检查登录确认状态失败: {e}")
                import traceback
                logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            
            # 等待2秒后再次检查
            await asyncio.sleep(2)
            timeout -= 2
        
        logger.error("[WX849] 等待登录确认超时")
        return False

    def _set_logged_in_state(self, wxid):
        """设置登录成功状态"""
        self.wxid = wxid
        self.user_id = wxid
        self.is_logged_in = True
        
        # 同步设置bot的wxid属性，确保消息获取不会失败
        if hasattr(self.bot, 'wxid'):
            self.bot.wxid = wxid
            logger.info(f"[WX849] 已同步设置bot.wxid = {wxid}")
        else:
            logger.error(f"[WX849] bot对象没有wxid属性，可能导致消息获取失败")

        # 异步获取用户资料
        threading.Thread(target=lambda: asyncio.run(self._get_user_profile())).start()

    async def _get_user_profile(self):
        """获取用户资料"""
        try:
            profile = await self.bot.get_profile()
            if profile and isinstance(profile, dict):
                userinfo = profile.get("userInfo", {})
                if isinstance(userinfo, dict):
                    if "NickName" in userinfo and isinstance(userinfo["NickName"], dict) and "string" in userinfo["NickName"]:
                        self.name = userinfo["NickName"]["string"]
                    elif "nickname" in userinfo:
                        self.name = userinfo["nickname"]
                    elif "nickName" in userinfo:
                        self.name = userinfo["nickName"]
                    else:
                        self.name = self.wxid
                    logger.info(f"[WX849] 获取到用户昵称: {self.name}")
                    return
            
            self.name = self.wxid
            logger.warning(f"[WX849] 无法解析用户资料，使用wxid作为昵称: {self.wxid}")
        except Exception as e:
            self.name = self.wxid
            logger.error(f"[WX849] 获取用户资料失败: {e}")

    async def _message_listener(self):
        """消息监听器"""
        logger.info("[WX849] 开始监听消息...")
        error_count = 0
        login_error_count = 0  # 跟踪登录错误计数
        
        while self.is_running:
            try:
                # 获取新消息
                try:
                    # 注释掉频繁打印的调试日志
                    # logger.debug("[WX849] 正在获取新消息...")
                    messages = await self.bot.get_new_message()
                    # 重置错误计数
                    error_count = 0
                    login_error_count = 0  # 重置登录错误计数
                except Exception as e:
                    error_count += 1
                    error_msg = str(e)
                    
                    # 检查是否是登录相关错误
                    if "请先登录" in error_msg or "您已退出微信" in error_msg or "登录已失效" in error_msg or "Please login first" in error_msg:
                        login_error_count += 1
                        # 记录更详细的日志信息
                        logger.error(f"[WX849] 获取消息出错，登录已失效: {e}")
                        
                        # 添加客户端状态信息
                        logger.error(f"[WX849] 客户端状态 - wxid: {getattr(self.bot, 'wxid', '未知')}")
                        logger.error(f"[WX849] 客户端状态 - 本地wxid: {self.wxid}")
                        logger.error(f"[WX849] 客户端状态 - API路径前缀: {getattr(self.bot, 'api_path_prefix', '未知')}")
                        logger.error(f"[WX849] 客户端状态 - 服务器: {getattr(self.bot, 'ip', '未知')}:{getattr(self.bot, 'port', '未知')}")
                        
                        # 获取API客户端类型
                        client_type = self.bot.__class__.__name__
                        client_module = self.bot.__class__.__module__
                        logger.error(f"[WX849] 客户端类型: {client_module}.{client_type}")
                        
                        # 尝试自动修复wxid不一致问题
                        if hasattr(self.bot, 'wxid') and self.wxid and (not self.bot.wxid or self.bot.wxid != self.wxid):
                            logger.warning(f"[WX849] 检测到wxid不一致，尝试修复: self.wxid={self.wxid}, bot.wxid={self.bot.wxid}")
                            self.bot.wxid = self.wxid
                            logger.info(f"[WX849] 已同步设置bot.wxid = {self.wxid}")
                            # 延迟执行下次重试，避免立即失败
                            await asyncio.sleep(2)
                            continue
                        
                        # 获取异常详细信息
                        import traceback
                        logger.error(f"[WX849] 异常详细堆栈:\n{traceback.format_exc()}")
                        
                        # 检查客户端的get_new_message方法
                        import inspect
                        if hasattr(self.bot, 'get_new_message') and callable(getattr(self.bot, 'get_new_message')):
                            try:
                                source = inspect.getsource(self.bot.get_new_message)
                                logger.debug(f"[WX849] get_new_message方法实现:\n{source}")
                            except Exception as source_err:
                                logger.debug(f"[WX849] 无法获取get_new_message源码: {source_err}")
                    else:
                        # 其他错误正常记录
                        logger.error(f"[WX849] 获取消息出错: {e}")
                        # 记录异常堆栈
                        import traceback
                        logger.error(f"[WX849] 异常堆栈: {traceback.format_exc()}")
                    
                    await asyncio.sleep(5)  # 出错后等待一段时间再重试
                    continue
                
                # 如果获取到消息，则处理
                if messages:
                    for idx, msg in enumerate(messages):
                        try:
                            logger.debug(f"[WX849] 处理第 {idx+1}/{len(messages)} 条消息")
                            # 判断是否是群消息
                            is_group = False
                            # 检查多种可能的群聊标识字段
                            if "roomId" in msg and msg["roomId"]:
                                is_group = True
                            elif "toUserName" in msg and msg["toUserName"] and msg["toUserName"].endswith("@chatroom"):
                                is_group = True
                            elif "ToUserName" in msg and msg["ToUserName"] and msg["ToUserName"].endswith("@chatroom"):
                                is_group = True
                            
                            if is_group:
                                logger.debug(f"[WX849] 识别为群聊消息")
                            else:
                                logger.debug(f"[WX849] 识别为私聊消息")
                            
                            # 创建消息对象
                            cmsg = WX849Message(msg, is_group)
                            
                            # 处理消息
                            if is_group:
                                await self.handle_group(cmsg)
                            else:
                                await self.handle_single(cmsg)
                        except Exception as e:
                            logger.error(f"[WX849] 处理消息出错: {e}")
                            # 打印完整的异常堆栈
                            import traceback
                            logger.error(f"[WX849] 异常堆栈: {traceback.format_exc()}")
                
                # 休眠一段时间
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[WX849] 消息监听器出错: {e}")
                # 打印完整的异常堆栈
                import traceback
                logger.error(f"[WX849] 异常堆栈: {traceback.format_exc()}")
                await asyncio.sleep(5)  # 出错后等待一段时间再重试

    def startup(self):
        """启动函数"""
        logger.info("[WX849] 正在启动...")
        
        # 创建事件循环
        loop = asyncio.new_event_loop()
        
        # 定义启动任务
        async def startup_task():
            # 初始化机器人（登录）
            login_success = await self._initialize_bot()
            if login_success:
                logger.info("[WX849] 登录成功，准备启动消息监听...")
                self.is_running = True
                # 启动消息监听
                await self._message_listener()
            else:
                logger.error("[WX849] 初始化失败")
        
        # 在新线程中运行事件循环
        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(startup_task())
        
        thread = threading.Thread(target=run_loop)
        thread.daemon = True
        thread.start()

    @_check
    async def handle_single(self, cmsg: ChatMessage):
        """处理私聊消息"""
        try:
            # 处理消息内容和类型
            await self._process_message(cmsg)
            
            # 只记录关键消息信息，减少日志输出
            if conf().get("log_level", "INFO") != "ERROR":
                logger.debug(f"[WX849] 私聊消息 - 类型: {cmsg.ctype}, ID: {cmsg.msg_id}, 内容: {cmsg.content[:20]}...")
            
            # 根据消息类型处理
            if cmsg.ctype == ContextType.VOICE and conf().get("speech_recognition") != True:
                logger.debug("[WX849] 语音识别功能未启用，跳过处理")
                return
            
            # 检查前缀匹配
            if cmsg.ctype == ContextType.TEXT:
                single_chat_prefix = conf().get("single_chat_prefix", [""])
                # 日志记录前缀配置，方便调试
                logger.debug(f"[WX849] 单聊前缀配置: {single_chat_prefix}")
                match_prefix = None
                for prefix in single_chat_prefix:
                    if prefix and cmsg.content.startswith(prefix):
                        logger.debug(f"[WX849] 匹配到前缀: {prefix}")
                        match_prefix = prefix
                        # 去除前缀
                        cmsg.content = cmsg.content[len(prefix):].strip()
                        logger.debug(f"[WX849] 去除前缀后的内容: {cmsg.content}")
                        break
                
                # 记录是否匹配
                if not match_prefix and single_chat_prefix and "" not in single_chat_prefix:
                    logger.debug(f"[WX849] 未匹配到前缀，消息被过滤: {cmsg.content}")
                    # 如果没有匹配到前缀且配置中没有空前缀，则直接返回，不处理该消息
                    return
            
            # 生成上下文
            context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=False, msg=cmsg)
            if context:
                self.produce(context)
            else:
                logger.debug(f"[WX849] 生成上下文失败，跳过处理")
        except Exception as e:
            logger.error(f"[WX849] 处理私聊消息异常: {e}")
            if conf().get("log_level", "INFO") == "DEBUG":
                import traceback
                logger.debug(f"[WX849] 异常堆栈: {traceback.format_exc()}")

    @_check
    async def handle_group(self, cmsg: ChatMessage):
        """处理群聊消息"""
        try:
            # 添加日志，记录处理前的消息基本信息
            logger.debug(f"[WX849] 开始处理群聊消息 - ID:{cmsg.msg_id} 类型:{cmsg.msg_type} 从:{cmsg.from_user_id}")
            
            # 处理消息内容和类型
            await self._process_message(cmsg)
            
            # 只记录关键消息信息，减少日志输出
            if conf().get("log_level", "INFO") != "ERROR":
                logger.debug(f"[WX849] 群聊消息 - 类型: {cmsg.ctype}, 群ID: {cmsg.other_user_id}")
            
            # 根据消息类型处理
            if cmsg.ctype == ContextType.VOICE and conf().get("group_speech_recognition") != True:
                logger.debug("[WX849] 群聊语音识别功能未启用，跳过处理")
                return
            
            # 检查白名单
            if cmsg.from_user_id and hasattr(cmsg, 'from_user_id'):
                group_white_list = conf().get("group_name_white_list", ["ALL_GROUP"])
                # 检查是否启用了白名单
                if "ALL_GROUP" not in group_white_list:
                    # 获取群名
                    group_name = None
                    try:
                        # 使用同步方式获取群名，避免事件循环嵌套
                        chatrooms_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", 'wx849_rooms.json')
                        
                        if os.path.exists(chatrooms_file):
                            try:
                                with open(chatrooms_file, 'r', encoding='utf-8') as f:
                                    chatrooms_info = json.load(f)
                                
                                if cmsg.from_user_id in chatrooms_info:
                                    group_name = chatrooms_info[cmsg.from_user_id].get("nickName")
                                    if group_name:
                                        logger.debug(f"[WX849] 从缓存获取到群名: {group_name}")
                            except Exception as e:
                                logger.error(f"[WX849] 读取群聊缓存失败: {e}")
                        
                        # 如果没有从缓存获取到群名，使用群ID作为备用
                        if not group_name:
                            group_name = cmsg.from_user_id
                            logger.debug(f"[WX849] 没有找到群名，使用群ID: {group_name}")
                        
                        logger.debug(f"[WX849] 群聊白名单检查 - 群名: {group_name}")
                    except Exception as e:
                        logger.error(f"[WX849] 获取群名称失败: {e}")
                        group_name = cmsg.from_user_id
                    
                    # 检查群名是否在白名单中
                    if group_name and group_name not in group_white_list:
                        # 使用群ID再次检查
                        if cmsg.from_user_id not in group_white_list:
                            logger.info(f"[WX849] 群聊不在白名单中，跳过处理: {group_name}")
                            return
                    
                    logger.debug(f"[WX849] 群聊通过白名单检查: {group_name or cmsg.from_user_id}")
            
            # 检查前缀匹配
            trigger_proceed = False
            if cmsg.ctype == ContextType.TEXT:
                group_chat_prefix = conf().get("group_chat_prefix", [])
                group_chat_keyword = conf().get("group_chat_keyword", [])
                
                # 日志记录前缀配置，方便调试
                logger.debug(f"[WX849] 群聊前缀配置: {group_chat_prefix}")
                logger.debug(f"[WX849] 群聊关键词配置: {group_chat_keyword}")
                
                # 检查前缀匹配
                for prefix in group_chat_prefix:
                    if prefix and cmsg.content.startswith(prefix):
                        logger.debug(f"[WX849] 群聊匹配到前缀: {prefix}")
                        # 去除前缀
                        cmsg.content = cmsg.content[len(prefix):].strip()
                        logger.debug(f"[WX849] 去除前缀后的内容: {cmsg.content}")
                        trigger_proceed = True
                        break
                
                # 检查关键词匹配
                if not trigger_proceed and group_chat_keyword:
                    for keyword in group_chat_keyword:
                        if keyword and keyword in cmsg.content:
                            logger.debug(f"[WX849] 群聊匹配到关键词: {keyword}")
                            trigger_proceed = True
                            break
                
                # 检查是否@了机器人（增强版）
                if not trigger_proceed and (cmsg.at_list or cmsg.content.find("@") >= 0):
                    logger.debug(f"[WX849] @列表: {cmsg.at_list}, 机器人wxid: {self.wxid}")
                    
                    # 检查at_list中是否包含机器人wxid
                    at_matched = False
                    if cmsg.at_list and self.wxid in cmsg.at_list:
                        at_matched = True
                        logger.debug(f"[WX849] 在at_list中匹配到机器人wxid: {self.wxid}")
                    
                    # 如果at_list为空，或者at_list中没有找到机器人wxid，则检查消息内容中是否直接包含@机器人的文本
                    if not at_matched and cmsg.content:
                        # 获取可能的机器人名称
                        robot_names = []
                        if self.name:
                            robot_names.append(self.name)
                        if hasattr(cmsg, 'self_display_name') and cmsg.self_display_name:
                            robot_names.append(cmsg.self_display_name)
                            
                        # 检查消息中是否包含@机器人名称
                        for name in robot_names:
                            at_text = f"@{name}"
                            if at_text in cmsg.content:
                                at_matched = True
                                logger.debug(f"[WX849] 在消息内容中直接匹配到@机器人: {at_text}")
                                break
                    
                    # 处理多种可能的@格式
                    if at_matched:
                        # 尝试移除不同格式的@文本
                        original_content = cmsg.content
                        at_patterns = []
                        
                        # 添加可能的@格式
                        if self.name:
                            at_patterns.extend([
                                f"@{self.name} ",  # 带空格
                                f"@{self.name}\u2005",  # 带特殊空格
                                f"@{self.name}",  # 不带空格
                            ])
                        
                        # 检查是否存在自定义的群内昵称
                        if hasattr(cmsg, 'self_display_name') and cmsg.self_display_name:
                            at_patterns.extend([
                                f"@{cmsg.self_display_name} ",  # 带空格
                                f"@{cmsg.self_display_name}\u2005",  # 带特殊空格
                                f"@{cmsg.self_display_name}",  # 不带空格
                            ])
                        
                        # 按照优先级尝试移除@文本
                        for pattern in at_patterns:
                            if pattern in cmsg.content:
                                cmsg.content = cmsg.content.replace(pattern, "", 1).strip()
                                logger.debug(f"[WX849] 匹配到@模式: {pattern}")
                                logger.debug(f"[WX849] 去除@后的内容: {cmsg.content}")
                                break
                        
                        # 如果没有匹配到任何@模式，但确实在at_list中找到了机器人或内容中包含@
                        # 尝试使用正则表达式移除通用@格式
                        if cmsg.content == original_content and at_matched:
                            import re
                            # 匹配形如"@任何内容 "的模式
                            at_pattern = re.compile(r'@[^\s]+[\s\u2005]+')
                            cmsg.content = at_pattern.sub("", cmsg.content, 1).strip()
                            logger.debug(f"[WX849] 使用正则表达式去除@后的内容: {cmsg.content}")
                        
                        trigger_proceed = True
                
                # 记录是否需要处理
                if not trigger_proceed:
                    logger.debug(f"[WX849] 群聊消息未匹配触发条件，跳过处理: {cmsg.content}")
                    return
            
            # 生成上下文
            context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=True, msg=cmsg)
            if context:
                self.produce(context)
            else:
                logger.debug(f"[WX849] 生成群聊上下文失败，跳过处理")
        except Exception as e:
            error_msg = str(e)
            # 添加更详细的错误日志信息
            logger.error(f"[WX849] 处理群聊消息异常: {error_msg}")
            logger.error(f"[WX849] 消息内容: {getattr(cmsg, 'content', '未知')[:100]}")
            logger.error(f"[WX849] 消息类型: {getattr(cmsg, 'msg_type', '未知')}")
            logger.error(f"[WX849] 上下文类型: {getattr(cmsg, 'ctype', '未知')}")
            
            # 记录完整的异常堆栈
            import traceback
            logger.error(f"[WX849] 异常堆栈: {traceback.format_exc()}")

    async def _process_message(self, cmsg):
        """处理消息内容和类型"""
        # 处理消息类型
        msg_type = cmsg.msg_type
        if not msg_type and "Type" in cmsg.msg:
            msg_type = cmsg.msg["Type"]
        
        # 尝试获取机器人在群内的昵称
        if cmsg.is_group and not cmsg.self_display_name:
            try:
                # 从缓存中查询群成员详情
                tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp")
                chatrooms_file = os.path.join(tmp_dir, 'wx849_rooms.json')
                
                if os.path.exists(chatrooms_file):
                    try:
                        with open(chatrooms_file, 'r', encoding='utf-8') as f:
                            chatrooms_info = json.load(f)
                        
                        if cmsg.from_user_id in chatrooms_info:
                            room_info = chatrooms_info[cmsg.from_user_id]
                            
                            # 在成员中查找机器人的信息
                            if "members" in room_info and isinstance(room_info["members"], list):
                                for member in room_info["members"]:
                                    if member.get("UserName") == self.wxid:
                                        # 优先使用群内显示名称
                                        if member.get("DisplayName"):
                                            cmsg.self_display_name = member.get("DisplayName")
                                            logger.debug(f"[WX849] 从群成员缓存中获取到机器人群内昵称: {cmsg.self_display_name}")
                                            break
                                        # 其次使用昵称
                                        elif member.get("NickName"):
                                            cmsg.self_display_name = member.get("NickName")
                                            logger.debug(f"[WX849] 从群成员缓存中获取到机器人昵称: {cmsg.self_display_name}")
                                            break
                    except Exception as e:
                        logger.error(f"[WX849] 读取群成员缓存失败: {e}")
                
                # 如果缓存中没有找到，使用机器人名称
                if not cmsg.self_display_name:
                    cmsg.self_display_name = self.name
                    logger.debug(f"[WX849] 使用机器人名称作为群内昵称: {cmsg.self_display_name}")
            except Exception as e:
                logger.error(f"[WX849] 获取机器人群内昵称失败: {e}")
        
        # 根据消息类型进行处理
        if msg_type in [1, "1", "Text"]:
            self._process_text_message(cmsg)
        elif msg_type in [3, "3", "Image"]:
            await self._process_image_message(cmsg)
        elif msg_type in [34, "34", "Voice"]:
            self._process_voice_message(cmsg)
        elif msg_type in [43, "43", "Video"]:
            self._process_video_message(cmsg)
        elif msg_type in [47, "47", "Emoji"]:
            self._process_emoji_message(cmsg)
        elif msg_type in [49, "49", "App"]:
            self._process_xml_message(cmsg)
        elif msg_type in [10000, "10000", "System"]:
            self._process_system_message(cmsg)
        else:
            # 默认类型处理
            cmsg.ctype = ContextType.UNKNOWN
            logger.warning(f"[WX849] 未知消息类型: {msg_type}, 内容: {cmsg.content[:100]}")
        
        # 检查消息是否来自群聊
        if cmsg.is_group or cmsg.from_user_id.endswith("@chatroom"):
            # 增强的群消息发送者提取逻辑
            # 尝试多种可能的格式解析发送者信息
            sender_extracted = False
            
            # 方法1: 尝试解析完整的格式 "wxid:\n消息内容"
            split_content = cmsg.content.split(":\n", 1)
            if len(split_content) > 1 and split_content[0] and not split_content[0].startswith("<"):
                cmsg.sender_wxid = split_content[0]
                cmsg.content = split_content[1]
                sender_extracted = True
                logger.debug(f"[WX849] 群聊发送者提取(方法1): {cmsg.sender_wxid}")
            
            # 方法2: 尝试解析简单的格式 "wxid:消息内容"
            #if not sender_extracted:
            #    split_content = cmsg.content.split(":", 1)
            #    if len(split_content) > 1 and split_content[0] and not split_content[0].startswith("<"):
            #        cmsg.sender_wxid = split_content[0]
            #        cmsg.content = split_content[1]
            #        sender_extracted = True
            #        logger.debug(f"[WX849] 群聊发送者提取(方法2): {cmsg.sender_wxid}")
            
            # 方法3: 尝试从回复XML中提取
            if not sender_extracted and cmsg.content and cmsg.content.startswith("<"):
                try:
                    # 解析XML内容
                    root = ET.fromstring(cmsg.content)
                    
                    # 查找不同类型的XML中可能存在的发送者信息
                    if root.tag == "msg":
                        # 常见的XML消息格式
                        sender_node = root.find(".//username")
                        if sender_node is not None and sender_node.text:
                            cmsg.sender_wxid = sender_node.text
                            sender_extracted = True
                            logger.debug(f"[WX849] 群聊发送者从XML提取: {cmsg.sender_wxid}")
                        
                        # 尝试其他可能的标签
                        if not sender_extracted:
                            for tag in ["fromusername", "sender", "from"]:
                                sender_node = root.find(f".//{tag}")
                                if sender_node is not None and sender_node.text:
                                    cmsg.sender_wxid = sender_node.text
                                    sender_extracted = True
                                    logger.debug(f"[WX849] 群聊发送者从XML({tag})提取: {cmsg.sender_wxid}")
                                    break
                except Exception as e:
                    logger.error(f"[WX849] 从XML提取群聊发送者失败: {e}")
            
            # 方法4: 尝试从其它字段提取
            if not sender_extracted:
                for key in ["SenderUserName", "sender", "senderId", "fromUser"]:
                    if key in cmsg.msg and cmsg.msg[key]:
                        cmsg.sender_wxid = str(cmsg.msg[key])
                        sender_extracted = True
                        logger.debug(f"[WX849] 群聊发送者从字段提取({key}): {cmsg.sender_wxid}")
                        break
            
            # 如果仍然无法提取，设置为默认值但不要留空
            if not sender_extracted or not cmsg.sender_wxid:
                cmsg.sender_wxid = f"未知用户_{cmsg.from_user_id}"
                logger.debug(f"[WX849] 无法提取群聊发送者，使用默认值: {cmsg.sender_wxid}")
            
            # 设置other_user_id为群ID，确保它不为None
            cmsg.other_user_id = cmsg.from_user_id
            
            # 设置actual_user_id为发送者wxid
            cmsg.actual_user_id = cmsg.sender_wxid
            
            # 异步获取发送者昵称并设置actual_user_nickname
            # 但现在我们无法在同步方法中直接调用异步方法，所以先使用wxid
            cmsg.actual_user_nickname = cmsg.sender_wxid
            
            # 启动异步任务获取昵称并更新actual_user_nickname
            threading.Thread(target=lambda: asyncio.run(self._update_nickname_async(cmsg))).start()
            
            logger.debug(f"[WX849] 设置实际发送者信息: actual_user_id={cmsg.actual_user_id}, actual_user_nickname={cmsg.actual_user_nickname}")
        else:
            # 私聊消息
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False
            
            # 私聊消息也设置actual_user_id和actual_user_nickname
            cmsg.actual_user_id = cmsg.from_user_id
            cmsg.actual_user_nickname = cmsg.from_user_id
            logger.debug(f"[WX849] 设置私聊发送者信息: actual_user_id={cmsg.actual_user_id}, actual_user_nickname={cmsg.actual_user_nickname}")

    async def _update_nickname_async(self, cmsg):
        """异步更新消息中的昵称信息"""
        if cmsg.is_group and cmsg.from_user_id.endswith("@chatroom"):
            nickname = await self._get_chatroom_member_nickname(cmsg.from_user_id, cmsg.sender_wxid)
            if nickname and nickname != cmsg.actual_user_nickname:
                cmsg.actual_user_nickname = nickname
                logger.debug(f"[WX849] 异步更新了发送者昵称: {nickname}")

    def _process_text_message(self, cmsg):
        """处理文本消息"""
        import xml.etree.ElementTree as ET
        
        cmsg.ctype = ContextType.TEXT
        
        # 处理群聊/私聊消息发送者
        if cmsg.is_group or cmsg.from_user_id.endswith("@chatroom"):
            cmsg.is_group = True
            
            # 增强的群消息发送者提取逻辑
            # 尝试多种可能的格式解析发送者信息
            sender_extracted = False
            
            # 方法1: 尝试解析完整的格式 "wxid:\n消息内容"
            split_content = cmsg.content.split(":\n", 1)
            if len(split_content) > 1 and split_content[0] and not split_content[0].startswith("<"):
                cmsg.sender_wxid = split_content[0]
                cmsg.content = split_content[1]
                sender_extracted = True
                logger.debug(f"[WX849] 群聊发送者提取(方法1): {cmsg.sender_wxid}")
            
            # 方法2: 尝试解析简单的格式 "wxid:消息内容"
            #if not sender_extracted:
            #    split_content = cmsg.content.split(":", 1)
            #    if len(split_content) > 1 and split_content[0] and not split_content[0].startswith("<"):
            #        cmsg.sender_wxid = split_content[0]
            #        cmsg.content = split_content[1]
            #        sender_extracted = True
            #       logger.debug(f"[WX849] 群聊发送者提取(方法2): {cmsg.sender_wxid}")
            
            # 方法3: 尝试从MsgSource XML中提取
            if not sender_extracted:
                try:
                    msg_source = cmsg.msg.get("MsgSource", "")
                    if msg_source and (msg_source.startswith("<") or "<msgsource>" in msg_source.lower()):
                        try:
                            if "<msgsource>" not in msg_source.lower():
                                msg_source = f"<msgsource>{msg_source}</msgsource>"
                            root = ET.fromstring(msg_source)
                            # 尝试提取不同可能的发送者标签
                            for tag in ["username", "nickname", "alias", "fromusername"]:
                                elem = root.find(f"./{tag}")
                                if elem is not None and elem.text:
                                    cmsg.sender_wxid = elem.text
                                    sender_extracted = True
                                    logger.debug(f"[WX849] 群聊发送者从MsgSource提取(标签:{tag}): {cmsg.sender_wxid}")
                                    break
                                    
                            # 同时尝试提取机器人在群内的昵称
                            for tag in ["selfDisplayName", "displayname", "nickname"]:
                                elem = root.find(f"./{tag}")
                                if elem is not None and elem.text:
                                    cmsg.self_display_name = elem.text
                                    logger.debug(f"[WX849] 从MsgSource中提取到机器人群内昵称: {cmsg.self_display_name}")
                                    break
                        except Exception as e:
                            logger.debug(f"[WX849] 从MsgSource提取发送者失败: {e}")
                except Exception as e:
                    logger.debug(f"[WX849] 尝试解析MsgSource时出错: {e}")
            
            # 方法4: 尝试从其它字段提取
            if not sender_extracted:
                for key in ["SenderUserName", "sender", "senderId", "fromUser"]:
                    if key in cmsg.msg and cmsg.msg[key]:
                        cmsg.sender_wxid = str(cmsg.msg[key])
                        sender_extracted = True
                        logger.debug(f"[WX849] 群聊发送者从字段提取({key}): {cmsg.sender_wxid}")
                        break
            
            # 如果仍然无法提取，设置为默认值但不要留空
            if not sender_extracted or not cmsg.sender_wxid:
                cmsg.sender_wxid = f"未知用户_{cmsg.from_user_id}"
                logger.debug(f"[WX849] 无法提取群聊发送者，使用默认值: {cmsg.sender_wxid}")
            
            # 设置other_user_id为群ID，确保它不为None
            cmsg.other_user_id = cmsg.from_user_id
            
            # 设置actual_user_id和actual_user_nickname
            cmsg.actual_user_id = cmsg.sender_wxid
            cmsg.actual_user_nickname = cmsg.sender_wxid
            logger.debug(f"[WX849] 设置实际发送者信息: actual_user_id={cmsg.actual_user_id}, actual_user_nickname={cmsg.actual_user_nickname}")
        else:
            # 私聊消息
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False
        
            # 私聊消息也设置actual_user_id和actual_user_nickname
            cmsg.actual_user_id = cmsg.from_user_id
            cmsg.actual_user_nickname = cmsg.from_user_id
            logger.debug(f"[WX849] 设置私聊发送者信息: actual_user_id={cmsg.actual_user_id}, actual_user_nickname={cmsg.actual_user_nickname}")
        
        # 解析@信息 - 多种方式解析
        try:
            # 方法1: 从MsgSource解析
            msg_source = cmsg.msg.get("MsgSource", "")
            if msg_source:
                try:
                    if "<msgsource>" not in msg_source.lower():
                        msg_source = f"<msgsource>{msg_source}</msgsource>"
                    root = ET.fromstring(msg_source)
                    ats_elem = root.find(".//atuserlist")
                    if ats_elem is not None and ats_elem.text:
                        cmsg.at_list = [x for x in ats_elem.text.strip(",").split(",") if x]
                        logger.debug(f"[WX849] 从MsgSource解析到@列表: {cmsg.at_list}")
                except Exception as e:
                    logger.debug(f"[WX849] 从MsgSource解析@列表失败: {e}")
            
            # 方法2: 从其他字段解析
            if not cmsg.at_list:
                for key in ["AtUserList", "at_list", "atlist"]:
                    if key in cmsg.msg:
                        at_value = cmsg.msg[key]
                        if isinstance(at_value, list):
                            cmsg.at_list = [str(x) for x in at_value if x]
                        elif isinstance(at_value, str):
                            cmsg.at_list = [x for x in at_value.strip(",").split(",") if x]
                        
                        if cmsg.at_list:
                            logger.debug(f"[WX849] 从字段{key}解析到@列表: {cmsg.at_list}")
                            break
            
            # 方法3: 从消息内容中检测@机器人
            if cmsg.is_group and not cmsg.at_list and "@" in cmsg.content:
                # 如果机器人有名称或群内昵称，检查是否被@
                if self.name and f"@{self.name}" in cmsg.content:
                    # 模拟添加自己到at_list
                    cmsg.at_list.append(self.wxid)
                    logger.debug(f"[WX849] 从消息内容检测到@机器人名称: {self.name}")
                elif hasattr(cmsg, 'self_display_name') and cmsg.self_display_name and f"@{cmsg.self_display_name}" in cmsg.content:
                    # 模拟添加自己到at_list
                    cmsg.at_list.append(self.wxid)
                    logger.debug(f"[WX849] 从消息内容检测到@机器人群内昵称: {cmsg.self_display_name}")
        except Exception as e:
            logger.debug(f"[WX849] 解析@列表失败: {e}")
            cmsg.at_list = []
        
        # 确保at_list不为空列表
        if not cmsg.at_list or (len(cmsg.at_list) == 1 and cmsg.at_list[0] == ""):
            cmsg.at_list = []
        
        # 输出日志
        logger.info(f"收到文本消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid} @:{cmsg.at_list} 内容:{cmsg.content}")

    async def _process_image_message(self, cmsg):
        """处理图片消息"""
        import xml.etree.ElementTree as ET
        import os
        import base64
        import asyncio
        import aiohttp
        import time
        import threading

        # 在这里不检查和标记图片消息，而是在图片下载完成后再标记
        # 这样可以确保图片消息被正确处理为IMAGE类型，而不是UNKNOWN类型

        cmsg.ctype = ContextType.IMAGE

        # 处理群聊/私聊消息发送者
        if cmsg.is_group or cmsg.from_user_id.endswith("@chatroom"):
            cmsg.is_group = True
            split_content = cmsg.content.split(":\n", 1)
            if len(split_content) > 1:
                cmsg.sender_wxid = split_content[0]
                cmsg.content = split_content[1]
            else:
                # 处理没有换行的情况
                split_content = cmsg.content.split(":", 1)
                if len(split_content) > 1:
                    cmsg.sender_wxid = split_content[0]
                    cmsg.content = split_content[1]
                else:
                    cmsg.content = split_content[0]
                    cmsg.sender_wxid = ""

            # 设置actual_user_id和actual_user_nickname
            cmsg.actual_user_id = cmsg.sender_wxid
            cmsg.actual_user_nickname = cmsg.sender_wxid
        else:
            # 私聊消息
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False

            # 私聊消息也设置actual_user_id和actual_user_nickname
            cmsg.actual_user_id = cmsg.from_user_id
            cmsg.actual_user_nickname = cmsg.from_user_id

        # 解析图片信息
        try:
            # 检查内容是否是XML
            if cmsg.content and (cmsg.content.startswith('<?xml') or cmsg.content.startswith("<msg>")):
                try:
                    root = ET.fromstring(cmsg.content)
                    img_element = root.find('img')
                    if img_element is not None:
                        cmsg.image_info = {
                            'aeskey': img_element.get('aeskey', ''),
                            'cdnmidimgurl': img_element.get('cdnmidimgurl', ''),
                            'length': img_element.get('length', '0'),
                            'md5': img_element.get('md5', '')
                        }
                        logger.debug(f"解析图片XML成功: aeskey={cmsg.image_info.get('aeskey', '')}, length={cmsg.image_info.get('length', '0')}, md5={cmsg.image_info.get('md5', '')}")
                    else:
                        # 如果找不到img元素，创建一个默认的image_info
                        cmsg.image_info = {
                            'aeskey': '',
                            'cdnmidimgurl': '',
                            'length': '0',
                            'md5': ''
                        }
                        logger.warning(f"[WX849] XML中未找到img元素，使用默认图片信息")
                except ET.ParseError as xml_err:
                    # XML解析失败，创建一个默认的image_info
                    logger.warning(f"[WX849] XML解析失败: {xml_err}, 使用默认图片信息")
                    cmsg.image_info = {
                        'aeskey': '',
                        'cdnmidimgurl': '',
                        'length': '0',
                        'md5': ''
                    }

                # 检查是否已经有图片路径
                if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
                    logger.info(f"[WX849] 图片已存在，路径: {cmsg.image_path}")
                else:
                    # 创建一个锁文件，防止重复下载
                    tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "images")
                    os.makedirs(tmp_dir, exist_ok=True)
                    lock_file = os.path.join(tmp_dir, f"img_{cmsg.msg_id}.lock")

                    # 检查锁文件是否存在
                    if os.path.exists(lock_file):
                        logger.info(f"[WX849] 图片 {cmsg.msg_id} 正在被其他线程下载，跳过")
                        return

                    # 创建锁文件
                    try:
                        with open(lock_file, "w") as f:
                            f.write(str(time.time()))
                    except Exception as e:
                        logger.error(f"[WX849] 创建锁文件失败: {e}")

                    # 尝试下载图片
                    try:
                        # Asynchronously download the image
                        result = await self._download_image(cmsg)
                    except Exception as e:
                        logger.error(f"[WX849] 下载图片失败: {e}")
                        logger.error(traceback.format_exc())
                    finally:
                        # 删除锁文件
                        try:
                            if os.path.exists(lock_file):
                                os.remove(lock_file)
                        except Exception as e:
                            logger.error(f"[WX849] 删除锁文件失败: {e}")
            else:
                # 如果内容不是XML，可能是已经下载好的图片路径
                if os.path.exists(cmsg.content):
                    cmsg.image_path = cmsg.content
                    logger.info(f"[WX849] 图片已存在，路径: {cmsg.image_path}")
                else:
                    logger.warning(f"[WX849] 图片内容既不是XML也不是有效路径: {cmsg.content[:100]}")
                    # 即使内容不是XML也不是有效路径，也创建一个默认的image_info
                    cmsg.image_info = {
                        'aeskey': '',
                        'cdnmidimgurl': '',
                        'length': '0',
                        'md5': ''
                    }
                    # 检查是否已经有图片路径
                    if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
                        logger.info(f"[WX849] 图片已存在，路径: {cmsg.image_path}")
                    else:
                        # 创建一个锁文件，防止重复下载
                        tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "images")
                        os.makedirs(tmp_dir, exist_ok=True)
                        lock_file = os.path.join(tmp_dir, f"img_{cmsg.msg_id}.lock")

                        # 检查锁文件是否存在
                        if os.path.exists(lock_file):
                            logger.info(f"[WX849] 图片 {cmsg.msg_id} 正在被其他线程下载，跳过")
                            return

                        # 创建锁文件
                        try:
                            with open(lock_file, "w") as f:
                                f.write(str(time.time()))
                        except Exception as e:
                            logger.error(f"[WX849] 创建锁文件失败: {e}")

                        # 尝试下载图片
                        try:
                            # Asynchronously download the image
                            result = await self._download_image(cmsg)
                        except Exception as e:
                            logger.error(f"[WX849] 下载图片失败: {e}")
                            logger.error(traceback.format_exc())
                        finally:
                            # 删除锁文件
                            try:
                                if os.path.exists(lock_file):
                                    os.remove(lock_file)
                            except Exception as e:
                                logger.error(f"[WX849] 删除锁文件失败: {e}")
        except Exception as e:
            logger.debug(f"解析图片消息失败: {e}, 内容: {cmsg.content[:100]}")
            logger.debug(f"详细错误: {traceback.format_exc()}")
            # 创建一个默认的image_info
            cmsg.image_info = {
                'aeskey': '',
                'cdnmidimgurl': '',
                'length': '0',
                'md5': ''
            }
            # 检查是否已经有图片路径
            if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
                logger.info(f"[WX849] 图片已存在，路径: {cmsg.image_path}")
            else:
                # 创建一个锁文件，防止重复下载
                tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "images")
                os.makedirs(tmp_dir, exist_ok=True)
                lock_file = os.path.join(tmp_dir, f"img_{cmsg.msg_id}.lock")

                # 检查锁文件是否存在
                if os.path.exists(lock_file):
                    logger.info(f"[WX849] 图片 {cmsg.msg_id} 正在被其他线程下载，跳过")
                    return

                # 创建锁文件
                try:
                    with open(lock_file, "w") as f:
                        f.write(str(time.time()))
                except Exception as e:
                    logger.error(f"[WX849] 创建锁文件失败: {e}")

                # 尝试下载图片
                try:
                    # Asynchronously download the image
                    result = await self._download_image(cmsg)
                except Exception as e:
                    logger.error(f"[WX849] 下载图片失败: {e}")
                    logger.error(traceback.format_exc())
                finally:
                    # 删除锁文件
                    try:
                        if os.path.exists(lock_file):
                            os.remove(lock_file)
                    except Exception as e:
                        logger.error(f"[WX849] 删除锁文件失败: {e}")

        # 输出日志 - 修改为显示完整XML内容
        logger.info(f"收到图片消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid}")

        # 记录最近收到的图片消息，用于识图等功能的关联
        session_id = cmsg.from_user_id if cmsg.is_group else cmsg.sender_wxid
        self.recent_image_msgs[session_id] = cmsg
        logger.info(f"[WX849] 已记录会话 {session_id} 的图片消息，可用于识图关联")

        # 如果已经有图片路径，更新消息内容为图片路径
        if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
            # 更新消息内容为图片路径
            cmsg.content = cmsg.image_path
            # 确保消息类型为IMAGE
            cmsg.ctype = ContextType.IMAGE
            # 图片已下载完成，记录日志
            logger.info(f"[WX849] 图片下载完成，保存到: {cmsg.image_path}")

            # 不再在这里生成上下文并发送到DOW框架
            # 图片消息只在_process_single_message_independently方法中处理一次

    async def _download_image(self, cmsg):
        """下载图片并设置本地路径"""
        try:
            # 检查是否已经有图片路径
            if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
                logger.info(f"[WX849] 图片已存在，路径: {cmsg.image_path}")
                return True

            # 创建临时目录
            tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "images")
            os.makedirs(tmp_dir, exist_ok=True)

            # 检查是否已经存在相同的图片文件
            msg_id = cmsg.msg_id
            existing_files = [f for f in os.listdir(tmp_dir) if f.startswith(f"img_{msg_id}_")]

            if existing_files:
                # 找到最新的文件
                latest_file = sorted(existing_files, key=lambda x: os.path.getmtime(os.path.join(tmp_dir, x)), reverse=True)[0]
                existing_path = os.path.join(tmp_dir, latest_file)

                # 检查文件是否有效
                if os.path.exists(existing_path) and os.path.getsize(existing_path) > 0:
                    try:
                        from PIL import Image
                        try:
                            # 尝试打开图片文件
                            with Image.open(existing_path) as img:
                                # 获取图片格式和大小
                                img_format = img.format
                                img_size = img.size
                                logger.info(f"[WX849] 图片已存在且有效: 格式={img_format}, 大小={img_size}")

                                # 设置图片本地路径
                                cmsg.image_path = existing_path
                                cmsg.content = existing_path
                                cmsg.ctype = ContextType.IMAGE
                                cmsg._prepared = True

                                logger.info(f"[WX849] 使用已存在的图片文件: {existing_path}")
                                return True
                        except Exception as img_err:
                            logger.warning(f"[WX849] 已存在的图片文件无效，重新下载: {img_err}")
                    except ImportError:
                        # 如果PIL库未安装，假设文件有效
                        if os.path.getsize(existing_path) > 10000:  # 至少10KB
                            cmsg.image_path = existing_path
                            cmsg.content = existing_path
                            cmsg.ctype = ContextType.IMAGE
                            cmsg._prepared = True

                            logger.info(f"[WX849] 使用已存在的图片文件: {existing_path}")
                            return True

            # 生成图片文件名
            image_filename = f"img_{cmsg.msg_id}_{int(time.time())}.jpg"
            image_path = os.path.join(tmp_dir, image_filename)

            # 直接使用分段下载方法，不再尝试使用GetMsgImage
            logger.info(f"[WX849] 使用分段下载方法获取图片")
            result = await self._download_image_by_chunks(cmsg, image_path)
            return result

        except Exception as e:
            logger.error(f"[WX849] 下载图片过程中出错: {e}")
            logger.error(traceback.format_exc())
            return False

    async def _download_image_by_chunks(self, cmsg, image_path):
        """使用分段下载方法获取图片"""
        import traceback
        import asyncio
        from io import BytesIO
        from PIL import Image, UnidentifiedImageError

        try:
            # 1. 检查是否已经存在相同的有效图片文件
            tmp_dir = os.path.dirname(image_path)
            os.makedirs(tmp_dir, exist_ok=True)
            msg_id_str = str(cmsg.msg_id) # 确保是字符串
            existing_files = [f for f in os.listdir(tmp_dir) if f.startswith(f"img_{msg_id_str}_")]

            if existing_files:
                latest_file = sorted(existing_files, key=lambda x: os.path.getmtime(os.path.join(tmp_dir, x)), reverse=True)[0]
                existing_path = os.path.join(tmp_dir, latest_file)
                if os.path.exists(existing_path) and os.path.getsize(existing_path) > 0:
                    try:
                        with open(existing_path, "rb") as f_exist:
                            existing_bytes = f_exist.read()
                        with Image.open(BytesIO(existing_bytes)) as img_exist:
                            logger.info(f"[WX849] 使用已存在且有效的图片: {existing_path}, Format: {img_exist.format}, Size: {img_exist.size}")
                            cmsg.image_path = existing_path
                            cmsg.content = existing_path
                            cmsg.ctype = ContextType.IMAGE
                            cmsg._prepared = True
                            return True
                    except UnidentifiedImageError:
                        logger.warning(f"[WX849] 已存在的图片 {existing_path} 无法识别 (UnidentifiedImageError)，将重新下载。")
                    except Exception as img_err_exist:
                        logger.warning(f"[WX849] 已存在的图片 {existing_path} 无效 ({img_err_exist})，将重新下载。")
            
            # 2. 获取API配置及计算分块信息
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            api_port = conf().get("wx849_api_port", 9011)
            protocol_version = conf().get("wx849_protocol_version", "849")
            api_path_prefix = "/api" if protocol_version in ["855", "ipad"] else "/VXAPI"
            data_len = int(cmsg.image_info.get('length', '0')) or 229920 # 默认大小
            chunk_size = 65536
            num_chunks = (data_len + chunk_size - 1) // chunk_size or 1

            logger.info(f"[WX849] 开始分段下载图片至: {image_path}，总大小: {data_len} B，分 {num_chunks} 段")

            # 3. 分块下载逻辑
            all_chunks_data_list = []
            download_stream_successful = True

            for i in range(num_chunks):
                start_pos = i * chunk_size
                current_chunk_size = min(chunk_size, data_len - start_pos)
                if current_chunk_size <= 0 and data_len > start_pos :
                    current_chunk_size = data_len - start_pos
                elif current_chunk_size <= 0:
                    logger.warning(f"[WX849] 计算的 current_chunk_size ({current_chunk_size}) <=0，且无剩余数据。StartPos: {start_pos}, DataLen: {data_len}")
                    break 

                params = {
                    "MsgId": cmsg.msg_id,
                    "ToWxid": cmsg.from_user_id,
                    "Wxid": self.wxid,
                    "DataLen": data_len,
                    "CompressType": 0,
                    "Section": {"StartPos": start_pos, "DataLen": current_chunk_size}
                }
                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Tools/DownloadImg"
                logger.debug(f"[WX849] 下载分段 {i+1}/{num_chunks}: URL={api_url}, Params={params}")

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
                            if response.status != 200:
                                logger.error(f"[WX849] 下载分段 {i+1} 失败, HTTP状态码: {response.status}")
                                download_stream_successful = False; break
                            result = await response.json()
                            if not result.get("Success", False):
                                logger.error(f"[WX849] 下载分段 {i+1} API报告失败: {result.get('Message', '未知错误')}")
                                download_stream_successful = False; break
                            
                            data_payload = result.get("Data", {})
                            chunk_base64 = None
                            if isinstance(data_payload, dict):
                                if "buffer" in data_payload:
                                    chunk_base64 = data_payload["buffer"]
                                elif "data" in data_payload and isinstance(data_payload["data"], dict) and "buffer" in data_payload["data"]:
                                    chunk_base64 = data_payload["data"]["buffer"]
                                else:
                                    for field in ["Chunk", "Image", "Data", "FileData"]:
                                        if field in data_payload:
                                            chunk_base64 = data_payload.get(field); break
                            elif isinstance(data_payload, str):
                                chunk_base64 = data_payload
                            
                            if not chunk_base64 and isinstance(result, dict):
                                 for field in ["data", "Data", "FileData", "Image"]:
                                     if field in result and result.get(field): # Check if field exists and has a value
                                         chunk_base64 = result.get(field); break

                            if not chunk_base64:
                                logger.error(f"[WX849] 下载分段 {i+1} 失败: 响应中未找到图片数据。Response: {str(result)[:200]}")
                                download_stream_successful = False; break
                            
                            try:
                                if not isinstance(chunk_base64, str):
                                    if isinstance(chunk_base64, bytes):
                                        try:
                                            chunk_base64 = chunk_base64.decode('utf-8')
                                        except UnicodeDecodeError:
                                            logger.error(f"[WX849] 第 {i+1}/{num_chunks} 段 chunk_base64 是 bytes 但无法以 utf-8 解码。")
                                            raise ValueError("chunk_base64是bytes但无法解码为str")
                                    else:
                                        logger.error(f"[WX849] 第 {i+1}/{num_chunks} 段 chunk_base64 不是字符串或字节类型: {type(chunk_base64)}.")
                                        raise ValueError(f"chunk_base64不是字符串或字节类型: {type(chunk_base64)}")
                                
                                clean_base64 = chunk_base64.strip()
                                padding = (4 - len(clean_base64) % 4) % 4
                                clean_base64 += '=' * padding
                                chunk_data_bytes = base64.b64decode(clean_base64)
                                all_chunks_data_list.append(chunk_data_bytes)
                                logger.debug(f"[WX849] 第 {i+1}/{num_chunks} 段解码成功，大小: {len(chunk_data_bytes)} B")
                            except Exception as decode_err:
                                logger.error(f"[WX849] 第 {i+1}/{num_chunks} 段Base64解码失败: {decode_err}. Data (头100): {str(chunk_base64)[:100]}")
                                download_stream_successful = False; break
                except asyncio.TimeoutError:
                    logger.error(f"[WX849] 下载分段 {i+1} 超时。")
                    download_stream_successful = False; break
                except Exception as api_err:
                    logger.error(f"[WX849] 下载分段 {i+1} 发生API调用错误: {api_err}")
                    logger.error(traceback.format_exc())
                    download_stream_successful = False; break
            
            # 4. 数据写入、刷新与同步
            file_written_successfully = False
            if download_stream_successful and all_chunks_data_list:
                try:
                    with open(image_path, "wb") as f_write:
                        for chunk_piece in all_chunks_data_list:
                            f_write.write(chunk_piece)
                        f_write.flush()
                        os.fsync(f_write.fileno())
                    actual_file_size = os.path.getsize(image_path)
                    logger.info(f"[WX849] 所有分块成功写入并刷新到磁盘: {image_path}, 实际大小: {actual_file_size} B")
                    if actual_file_size == 0 and sum(len(c) for c in all_chunks_data_list) > 0 :
                        logger.error(f"[WX849] 警告：数据已下载 ({sum(len(c) for c in all_chunks_data_list)}B) 但写入文件后大小为0！Path: {image_path}")
                    else:
                        file_written_successfully = True
                except IOError as io_err_write:
                    logger.error(f"[WX849] 写入或刷新图片文件失败: {io_err_write}, Path: {image_path}")
                except Exception as e_write:
                    logger.error(f"[WX849] 写入文件时发生未知错误: {e_write}, Path: {image_path}")
                    logger.error(traceback.format_exc())
            elif not all_chunks_data_list and download_stream_successful:
                logger.warning(f"[WX849] 所有分块下载API调用成功，但未收集到任何数据块。")
            
            # 5. 图片验证阶段
            if file_written_successfully:
                await asyncio.sleep(0.1) 
                try:
                    with open(image_path, "rb") as f_read_verify:
                        image_bytes_for_verify = f_read_verify.read()
                    
                    if not image_bytes_for_verify:
                        logger.error(f"[WX849] 图片文件下载后读取为空: {image_path}")
                        raise UnidentifiedImageError("Downloaded image file is empty after read for verification.")

                    with Image.open(BytesIO(image_bytes_for_verify)) as img:
                        img_format = img.format
                        img_size = img.size
                        logger.info(f"[WX849] 图片验证成功 (PIL): 格式={img_format}, 大小={img_size}, 路径={image_path}")
                        cmsg.image_path = image_path
                        cmsg.content = image_path
                        cmsg.ctype = ContextType.IMAGE
                        cmsg._prepared = True
                        return True
                except UnidentifiedImageError as unident_err:
                    logger.error(f"[WX849] 图片验证失败 (PIL无法识别格式): {unident_err}, 文件: {image_path}")
                    if os.path.exists(image_path): os.remove(image_path)
                    cmsg._prepared = False
                    return False
                except AttributeError as attr_err:
                    err_str = str(attr_err)
                    # Catches "property 'mode' of 'JpegImageFile' object has no setter" 
                    # AND "'JpegImageFile' object has no attribute 'layers'" (another PIL internal error for some JPEGs)
                    if "property 'mode' of" in err_str and "object has no setter" in err_str or \
                       "'JpegImageFile' object has no attribute 'layers'" in err_str:
                        logger.warning(f"[WX849] 图片验证遇到特定AttributeError (可能是误报): {err_str}, 文件: {image_path}")
                        logger.warning(f"[WX849] 将保留文件 {image_path} 并标记为 \'未准备好\' (cmsg._prepared=False)，交由下游处理。")
                        cmsg.image_path = image_path
                        cmsg.content = image_path
                        cmsg.ctype = ContextType.IMAGE
                        cmsg._prepared = False 
                        return True 
                    else:
                        logger.error(f"[WX849] 图片验证失败 (非特定AttributeError): {attr_err}, 文件: {image_path}")
                        logger.error(traceback.format_exc())
                        if os.path.exists(image_path): os.remove(image_path)
                        cmsg._prepared = False
                        return False
                except ImportError:
                    logger.warning("[WX849] PIL (Pillow) 库未安装，无法对图片进行严格验证。")
                    fsize = os.path.getsize(image_path) if os.path.exists(image_path) else 0
                    if fsize > 10000:
                        logger.info(f"[WX849] 图片下载完成 (无PIL验证，大小: {fsize}B)，路径: {image_path}")
                        cmsg.image_path = image_path
                        cmsg.content = image_path
                        cmsg.ctype = ContextType.IMAGE
                        cmsg._prepared = True
                        return True
                    else:
                        logger.warning(f"[WX849] PIL未安装且文件大小 ({fsize}B) 过小，视为无效: {image_path}")
                        if os.path.exists(image_path): os.remove(image_path)
                        cmsg._prepared = False
                        return False
                except Exception as pil_verify_err:
                    logger.error(f"[WX849] 图片验证时发生未知PIL错误: {pil_verify_err}, 文件: {image_path}")
                    logger.error(traceback.format_exc())
                    if os.path.exists(image_path): os.remove(image_path)
                    cmsg._prepared = False
                    return False
            
            # 6. 最终失败路径
            logger.error(f"[WX849] 图片下载或验证未能成功。download_stream_successful={download_stream_successful}, file_written_successfully={file_written_successfully}, all_chunks_data_list is {'not ' if not all_chunks_data_list else ''}empty. Path: {image_path}")
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                    logger.info(f"[WX849] 已删除下载失败或验证失败的图片文件: {image_path}")
                except Exception as e_remove_final:
                    logger.error(f"[WX849] 删除失败的图片文件时出错: {e_remove_final}, Path: {image_path}")
            cmsg._prepared = False
            return False

        except Exception as outer_e:
            logger.critical(f"[WX849] _download_image_by_chunks 发生严重意外错误: {outer_e}")
            logger.critical(traceback.format_exc())
            current_image_path_for_cleanup = image_path 
            if current_image_path_for_cleanup and os.path.exists(current_image_path_for_cleanup):
                try:
                    os.remove(current_image_path_for_cleanup)
                    logger.info(f"[WX849] 意外错误后，已尝试删除图片文件: {current_image_path_for_cleanup}")
                except Exception as e_remove_outer: # pragma: no cover
                    logger.error(f"[WX849] 意外错误后，删除图片文件失败: {e_remove_outer}")
            if cmsg: # Check if cmsg is not None
                cmsg._prepared = False
            return False

            # 获取API配置
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            api_port = conf().get("wx849_api_port", 9011)
            protocol_version = conf().get("wx849_protocol_version", "849")

            # 确定API路径前缀
            if protocol_version == "855" or protocol_version == "ipad":
                api_path_prefix = "/api"
            else:
                api_path_prefix = "/VXAPI"

            # 估计图片大小
            data_len = int(cmsg.image_info.get('length', '0'))
            if data_len <= 0:
                data_len = 229920  # 默认大小

            # 分段大小
            chunk_size = 65536  # 64KB - 必须使用这个大小，API限制每次最多下载64KB

            # 计算分段数
            num_chunks = (data_len + chunk_size - 1) // chunk_size
            if num_chunks <= 0:
                num_chunks = 1  # 至少分1段

            # 不限制最大分段数，确保完整下载图片
            # 但记录一个警告，如果分段数过多
            if num_chunks > 20:
                logger.warning(f"[WX849] 图片分段数较多 ({num_chunks})，下载可能需要较长时间")

            logger.info(f"[WX849] 开始分段下载图片，总大小: {data_len} 字节，分 {num_chunks} 段下载")

            # 创建一个空文件
            with open(image_path, "wb") as f:
                pass

            # 分段下载
            all_chunks_success = True
            for i in range(num_chunks):
                start_pos = i * chunk_size
                current_chunk_size = min(chunk_size, data_len - start_pos)
                if current_chunk_size <= 0:
                    current_chunk_size = chunk_size

                # 构建API请求参数 - 使用与原始框架相同的格式
                params = {
                    "MsgId": cmsg.msg_id,
                    "ToWxid": cmsg.from_user_id,
                    "Wxid": self.wxid,
                    "DataLen": data_len,
                    "CompressType": 0,
                    "Section": {
                        "StartPos": start_pos,
                        "DataLen": current_chunk_size
                    }
                }

                logger.debug(f"[WX849] 尝试下载图片分段: MsgId={cmsg.msg_id}, ToWxid={cmsg.from_user_id}, DataLen={data_len}, StartPos={start_pos}, ChunkSize={current_chunk_size}")

                # 构建完整的API URL - 尝试不同的API路径
                # 首先尝试 /VXAPI/Tools/DownloadImg
                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Tools/DownloadImg"

                # 记录完整的请求URL和参数
                logger.debug(f"[WX849] 图片下载API URL: {api_url}")
                logger.debug(f"[WX849] 图片下载API参数: {params}")

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=params) as response:
                            if response.status != 200:
                                logger.error(f"[WX849] 下载图片分段失败, 状态码: {response.status}")
                                all_chunks_success = False
                                break

                            # 获取响应内容
                            result = await response.json()

                            # 检查响应是否成功
                            if not result.get("Success", False):
                                logger.error(f"[WX849] 下载图片分段失败: {result.get('Message', '未知错误')}")
                                all_chunks_success = False
                                break

                            # 提取图片数据 - 适应原始框架的响应格式
                            data = result.get("Data", {})

                            # 记录响应结构以便调试
                            if isinstance(data, dict):
                                logger.debug(f"[WX849] 响应Data字段包含键: {list(data.keys())}")

                            # 尝试不同的响应格式
                            chunk_base64 = None

                            # 参考 WechatAPI/Client/tool_extension.py 中的处理方式
                            if isinstance(data, dict):
                                # 如果是字典，尝试获取buffer字段
                                if "buffer" in data:
                                    logger.debug(f"[WX849] 从data.buffer字段获取图片数据")
                                    chunk_base64 = data.get("buffer")
                                elif "data" in data and isinstance(data["data"], dict) and "buffer" in data["data"]:
                                    logger.debug(f"[WX849] 从data.data.buffer字段获取图片数据")
                                    chunk_base64 = data["data"]["buffer"]
                                else:
                                    # 尝试其他可能的字段名
                                    for field in ["Chunk", "Image", "Data", "FileData", "data"]:
                                        if field in data:
                                            logger.debug(f"[WX849] 从data.{field}字段获取图片数据")
                                            chunk_base64 = data.get(field)
                                            break
                            elif isinstance(data, str):
                                # 如果直接返回字符串，可能就是base64数据
                                logger.debug(f"[WX849] Data字段是字符串，直接使用")
                                chunk_base64 = data

                            # 如果在data中没有找到，尝试在整个响应中查找
                            if not chunk_base64 and isinstance(result, dict):
                                for field in ["data", "Data", "FileData", "Image"]:
                                    if field in result:
                                        logger.debug(f"[WX849] 从result.{field}字段获取图片数据")
                                        chunk_base64 = result.get(field)
                                        break

                            if not chunk_base64:
                                logger.error(f"[WX849] 下载图片分段失败: 响应中无图片数据")
                                # 避免记录大量数据，只记录数据类型和长度
                                if isinstance(data, dict):
                                    logger.debug(f"[WX849] 响应数据类型: 字典, 键: {list(data.keys())}")
                                elif isinstance(data, str):
                                    logger.debug(f"[WX849] 响应数据类型: 字符串, 长度: {len(data)}")
                                else:
                                    logger.debug(f"[WX849] 响应数据类型: {type(data)}")
                                all_chunks_success = False
                                break

                            # 解码数据并保存图片分段
                            try:
                                # 尝试确定数据类型并正确处理
                                if isinstance(chunk_base64, str):
                                    # 尝试作为Base64解码
                                    try:
                                        # 修复：确保字符串是有效的Base64
                                        # 移除可能的非Base64字符
                                        clean_base64 = chunk_base64.strip()
                                        # 确保长度是4的倍数，如果不是，添加填充
                                        padding = 4 - (len(clean_base64) % 4) if len(clean_base64) % 4 != 0 else 0
                                        clean_base64 = clean_base64 + ('=' * padding)

                                        chunk_data = base64.b64decode(clean_base64)
                                        logger.debug(f"[WX849] 成功解码Base64数据，大小: {len(chunk_data)} 字节")
                                    except Exception as decode_err:
                                        logger.error(f"[WX849] Base64解码失败: {decode_err}")
                                        # 如果解码失败，记录错误并跳过这个分段
                                        logger.error(f"[WX849] 无法解码的数据: {chunk_base64[:100]}...")
                                        raise decode_err
                                elif isinstance(chunk_base64, bytes):
                                    # 已经是二进制数据，直接使用
                                    logger.debug(f"[WX849] 使用二进制数据，大小: {len(chunk_base64)} 字节")
                                    chunk_data = chunk_base64
                                elif isinstance(chunk_base64, dict):
                                    # 如果是字典，尝试从字典中提取数据
                                    logger.debug(f"[WX849] 数据是字典类型，键: {list(chunk_base64.keys())}")
                                    # 尝试从常见的键中获取数据
                                    found_data = False
                                    for key in ['data', 'Data', 'content', 'Content', 'image', 'Image']:
                                        if key in chunk_base64:
                                            data_value = chunk_base64[key]
                                            if isinstance(data_value, str):
                                                try:
                                                    # 清理和填充Base64字符串
                                                    clean_base64 = data_value.strip()
                                                    padding = 4 - (len(clean_base64) % 4) if len(clean_base64) % 4 != 0 else 0
                                                    clean_base64 = clean_base64 + ('=' * padding)

                                                    chunk_data = base64.b64decode(clean_base64)
                                                    logger.debug(f"[WX849] 从字典键 {key} 成功解码Base64数据，大小: {len(chunk_data)} 字节")
                                                    found_data = True
                                                    break
                                                except Exception as e:
                                                    logger.debug(f"[WX849] 从字典键 {key} 解码Base64失败: {e}")
                                                    continue
                                            elif isinstance(data_value, bytes):
                                                chunk_data = data_value
                                                logger.debug(f"[WX849] 从字典键 {key} 获取到二进制数据，大小: {len(data_value)} 字节")
                                                found_data = True
                                                break

                                    # 如果常见键中没有找到，尝试遍历所有键
                                    if not found_data:
                                        logger.debug(f"[WX849] 在常见键中未找到数据，尝试遍历所有键")
                                        for key, value in chunk_base64.items():
                                            if isinstance(value, str) and len(value) > 10:  # 只处理可能是Base64的长字符串
                                                try:
                                                    # 清理和填充Base64字符串
                                                    clean_base64 = value.strip()
                                                    padding = 4 - (len(clean_base64) % 4) if len(clean_base64) % 4 != 0 else 0
                                                    clean_base64 = clean_base64 + ('=' * padding)

                                                    chunk_data = base64.b64decode(clean_base64)
                                                    logger.debug(f"[WX849] 从字典键 {key} 成功解码Base64数据，大小: {len(chunk_data)} 字节")
                                                    found_data = True
                                                    break
                                                except Exception:
                                                    continue
                                            elif isinstance(value, bytes) and len(value) > 10:
                                                chunk_data = value
                                                logger.debug(f"[WX849] 从字典键 {key} 获取到二进制数据，大小: {len(value)} 字节")
                                                found_data = True
                                                break
                                            elif isinstance(value, dict) and len(value) > 0:
                                                # 递归处理嵌套字典
                                                logger.debug(f"[WX849] 发现嵌套字典，键: {key}，尝试递归处理")
                                                for subkey, subvalue in value.items():
                                                    if isinstance(subvalue, str) and len(subvalue) > 10:
                                                        try:
                                                            # 清理和填充Base64字符串
                                                            clean_base64 = subvalue.strip()
                                                            padding = 4 - (len(clean_base64) % 4) if len(clean_base64) % 4 != 0 else 0
                                                            clean_base64 = clean_base64 + ('=' * padding)

                                                            chunk_data = base64.b64decode(clean_base64)
                                                            logger.debug(f"[WX849] 从嵌套字典键 {key}.{subkey} 成功解码Base64数据，大小: {len(chunk_data)} 字节")
                                                            found_data = True
                                                            break
                                                        except Exception:
                                                            continue
                                                    elif isinstance(subvalue, bytes) and len(subvalue) > 10:
                                                        chunk_data = subvalue
                                                        logger.debug(f"[WX849] 从嵌套字典键 {key}.{subkey} 获取到二进制数据，大小: {len(subvalue)} 字节")
                                                        found_data = True
                                                        break
                                                if found_data:
                                                    break

                                    # 如果仍然没有找到有效数据，尝试使用整个字典的字符串表示
                                    if not found_data:
                                        logger.warning(f"[WX849] 无法从字典中提取有效的图片数据，尝试使用默认空数据")
                                        # 创建一个空的JPEG图片数据（最小的有效JPEG文件）
                                        chunk_data = b'\xff\xd8\xff\xe0\x00\x10\x4a\x46\x49\x46\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xdb\x00\x43\x00\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\xff\xd9'
                                        logger.debug(f"[WX849] 使用默认空JPEG数据，大小: {len(chunk_data)} 字节")
                                else:
                                    # 其他类型，尝试转换为字符串后处理
                                    logger.warning(f"[WX849] 未知数据类型: {type(chunk_base64)}，尝试转换为字符串")
                                    try:
                                        # 尝试转换为字符串
                                        str_value = str(chunk_base64)
                                        if len(str_value) > 10:  # 只处理可能有意义的字符串
                                            try:
                                                # 清理和填充Base64字符串
                                                clean_base64 = str_value.strip()
                                                padding = 4 - (len(clean_base64) % 4) if len(clean_base64) % 4 != 0 else 0
                                                clean_base64 = clean_base64 + ('=' * padding)

                                                chunk_data = base64.b64decode(clean_base64)
                                                logger.debug(f"[WX849] 成功将未知类型转换为Base64并解码，大小: {len(chunk_data)} 字节")
                                            except Exception as e:
                                                logger.warning(f"[WX849] 无法将未知类型转换为Base64: {e}")
                                                # 使用默认空JPEG数据
                                                chunk_data = b'\xff\xd8\xff\xe0\x00\x10\x4a\x46\x49\x46\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xdb\x00\x43\x00\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\xff\xd9'
                                                logger.debug(f"[WX849] 使用默认空JPEG数据，大小: {len(chunk_data)} 字节")
                                        else:
                                            # 字符串太短，使用默认空JPEG数据
                                            chunk_data = b'\xff\xd8\xff\xe0\x00\x10\x4a\x46\x49\x46\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xdb\x00\x43\x00\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\xff\xd9'
                                            logger.debug(f"[WX849] 使用默认空JPEG数据，大小: {len(chunk_data)} 字节")
                                    except Exception as e:
                                        logger.error(f"[WX849] 处理未知数据类型失败: {e}")
                                        # 使用默认空JPEG数据
                                        chunk_data = b'\xff\xd8\xff\xe0\x00\x10\x4a\x46\x49\x46\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xdb\x00\x43\x00\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\xff\xd9'
                                        logger.debug(f"[WX849] 使用默认空JPEG数据，大小: {len(chunk_data)} 字节")

                                # 验证数据是否为有效的图片数据
                                if 'chunk_data' in locals() and chunk_data:
                                    # 追加到文件
                                    with open(image_path, "ab") as f:
                                        f.write(chunk_data)
                                    logger.debug(f"[WX849] 第 {i+1}/{num_chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                else:
                                    logger.error(f"[WX849] 第 {i+1}/{num_chunks} 段下载失败: 无有效数据")
                                    all_chunks_success = False
                                    break
                            except Exception as decode_err:
                                logger.error(f"[WX849] 解码Base64图片分段数据失败: {decode_err}")
                                all_chunks_success = False
                                break
                except Exception as api_err:
                    logger.error(f"[WX849] 调用图片分段API失败: {api_err}")
                    all_chunks_success = False
                    break

            if all_chunks_success:
                # 检查文件大小
                file_size = os.path.getsize(image_path)
                logger.info(f"[WX849] 分段下载图片成功，总大小: {file_size} 字节")

                # 如果文件大小与预期不符，记录警告
                if file_size < data_len * 0.8:  # 如果实际大小小于预期的80%
                    logger.warning(f"[WX849] 图片文件大小 ({file_size} 字节) 小于预期 ({data_len} 字节)，可能下载不完整")

                    # 如果文件太小，尝试再次下载缺失的部分
                    if file_size > 0 and file_size < data_len:
                        logger.info(f"[WX849] 尝试下载缺失的图片部分，从位置 {file_size} 开始")

                        # 计算剩余部分的分段数
                        remaining_size = data_len - file_size
                        remaining_chunks = (remaining_size + chunk_size - 1) // chunk_size

                        # 下载剩余部分
                        remaining_success = True
                        for i in range(remaining_chunks):
                            start_pos = file_size + i * chunk_size
                            current_chunk_size = min(chunk_size, data_len - start_pos)

                            if current_chunk_size <= 0:
                                break

                            # 构建API请求参数
                            params = {
                                "MsgId": cmsg.msg_id,
                                "ToWxid": cmsg.from_user_id,
                                "Wxid": self.wxid,
                                "DataLen": data_len,
                                "CompressType": 0,
                                "Section": {
                                    "StartPos": start_pos,
                                    "DataLen": current_chunk_size
                                }
                            }

                            logger.debug(f"[WX849] 尝试下载缺失的图片分段: MsgId={cmsg.msg_id}, StartPos={start_pos}, ChunkSize={current_chunk_size}")

                            try:
                                async with aiohttp.ClientSession() as session:
                                    async with session.post(api_url, json=params) as response:
                                        if response.status != 200:
                                            logger.error(f"[WX849] 下载缺失的图片分段失败, 状态码: {response.status}")
                                            remaining_success = False
                                            break

                                        result = await response.json()

                                        if not result.get("Success", False):
                                            logger.error(f"[WX849] 下载缺失的图片分段失败: {result.get('Message', '未知错误')}")
                                            remaining_success = False
                                            break

                                        # 提取图片数据
                                        data = result.get("Data", {})
                                        chunk_base64 = None

                                        # 参考 WechatAPI/Client/tool_extension.py 中的处理方式
                                        if isinstance(data, dict):
                                            if "buffer" in data:
                                                chunk_base64 = data.get("buffer")
                                            elif "data" in data and isinstance(data["data"], dict) and "buffer" in data["data"]:
                                                chunk_base64 = data["data"]["buffer"]

                                        if chunk_base64:
                                            try:
                                                chunk_data = base64.b64decode(chunk_base64)
                                                with open(image_path, "ab") as f:
                                                    f.write(chunk_data)
                                                logger.debug(f"[WX849] 缺失的图片分段下载成功，大小: {len(chunk_data)} 字节")
                                            except Exception as e:
                                                logger.error(f"[WX849] 解码缺失的图片分段数据失败: {e}")
                                                remaining_success = False
                                                break
                                        else:
                                            logger.error(f"[WX849] 下载缺失的图片分段失败: 响应中无图片数据")
                                            remaining_success = False
                                            break
                            except Exception as e:
                                logger.error(f"[WX849] 下载缺失的图片分段失败: {e}")
                                remaining_success = False
                                break

                        if remaining_success:
                            file_size = os.path.getsize(image_path)
                            logger.info(f"[WX849] 成功下载缺失的图片部分，最终大小: {file_size} 字节")

                # 检查文件是否存在且有效
                if os.path.exists(image_path) and os.path.getsize(image_path) > 0:
                    # 验证图片文件是否为有效的图片格式
                    try:
                        from PIL import Image
                        try:
                            # 尝试打开图片文件
                            with Image.open(image_path) as img:
                                # 获取图片格式和大小
                                img_format = img.format
                                img_size = img.size
                                logger.info(f"[WX849] 图片验证成功: 格式={img_format}, 大小={img_size}")
                        except Exception as img_err:
                            logger.error(f"[WX849] 图片验证失败，可能不是有效的图片文件: {img_err}")
                            # 尝试修复图片文件
                            try:
                                # 读取文件内容
                                with open(image_path, "rb") as f:
                                    img_data = f.read()

                                # 尝试查找JPEG文件头和尾部标记
                                jpg_header = b'\xff\xd8'
                                jpg_footer = b'\xff\xd9'

                                if img_data.startswith(jpg_header) and img_data.endswith(jpg_footer):
                                    logger.info(f"[WX849] 图片文件有效的JPEG头尾标记，但内部可能有损坏")
                                else:
                                    # 查找JPEG头部标记的位置
                                    header_pos = img_data.find(jpg_header)
                                    if header_pos >= 0:
                                        # 查找JPEG尾部标记的位置
                                        footer_pos = img_data.rfind(jpg_footer)
                                        if footer_pos > header_pos:
                                            # 提取有效的JPEG数据
                                            valid_data = img_data[header_pos:footer_pos+2]
                                            # 重写文件
                                            with open(image_path, "wb") as f:
                                                f.write(valid_data)
                                            logger.info(f"[WX849] 尝试修复图片文件，提取了 {len(valid_data)} 字节的有效JPEG数据")
                            except Exception as fix_err:
                                logger.error(f"[WX849] 尝试修复图片文件失败: {fix_err}")
                    except ImportError:
                        logger.warning(f"[WX849] PIL库未安装，无法验证图片有效性")

                    # 设置图片本地路径
                    cmsg.image_path = image_path

                    # 更新消息内容为图片路径，以便DOW框架处理
                    cmsg.content = image_path

                    # 确保消息类型为IMAGE
                    cmsg.ctype = ContextType.IMAGE

                    # 设置_prepared标志，表示图片已准备好
                    cmsg._prepared = True

                    # 图片已经在回调处理时发送到DOW框架，这里只记录日志
                    logger.info(f"[WX849] 图片下载完成，保存到: {cmsg.image_path}")

                    # 返回True表示下载成功
                    return True
                else:
                    logger.error(f"[WX849] 图片文件不存在或为空: {image_path}")
                    return False
        except Exception as e:
            logger.error(f"[WX849] 下载图片失败: {e}")
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")

    def _get_image(self, msg_id):
        """获取图片数据"""
        # 查找图片文件
        tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "images")

        # 查找匹配的图片文件
        if os.path.exists(tmp_dir):
            for filename in os.listdir(tmp_dir):
                if filename.startswith(f"img_{msg_id}_"):
                    image_path = os.path.join(tmp_dir, filename)
                    try:
                        # 验证图片文件是否为有效的图片格式
                        try:
                            from PIL import Image
                            try:
                                # 尝试打开图片文件
                                with Image.open(image_path) as img:
                                    # 获取图片格式和大小
                                    img_format = img.format
                                    img_size = img.size
                                    logger.info(f"[WX849] 图片验证成功: 格式={img_format}, 大小={img_size}")
                            except Exception as img_err:
                                logger.error(f"[WX849] 图片验证失败，可能不是有效的图片文件: {img_err}")
                                # 尝试修复图片文件
                                try:
                                    # 读取文件内容
                                    with open(image_path, "rb") as f:
                                        img_data = f.read()

                                    # 尝试查找JPEG文件头和尾部标记
                                    jpg_header = b'\xff\xd8'
                                    jpg_footer = b'\xff\xd9'

                                    if img_data.startswith(jpg_header) and img_data.endswith(jpg_footer):
                                        logger.info(f"[WX849] 图片文件有效的JPEG头尾标记，但内部可能有损坏")
                                    else:
                                        # 查找JPEG头部标记的位置
                                        header_pos = img_data.find(jpg_header)
                                        if header_pos >= 0:
                                            # 查找JPEG尾部标记的位置
                                            footer_pos = img_data.rfind(jpg_footer)
                                            if footer_pos > header_pos:
                                                # 提取有效的JPEG数据
                                                valid_data = img_data[header_pos:footer_pos+2]
                                                # 重写文件
                                                with open(image_path, "wb") as f:
                                                    f.write(valid_data)
                                                logger.info(f"[WX849] 尝试修复图片文件，提取了 {len(valid_data)} 字节的有效JPEG数据")
                                                # 返回修复后的数据
                                                return valid_data
                                except Exception as fix_err:
                                    logger.error(f"[WX849] 尝试修复图片文件失败: {fix_err}")
                        except ImportError:
                            logger.warning(f"[WX849] PIL库未安装，无法验证图片有效性")

                        # 读取图片文件
                        with open(image_path, "rb") as f:
                            image_data = f.read()
                            logger.info(f"[WX849] 成功读取图片文件: {image_path}, 大小: {len(image_data)} 字节")
                            return image_data
                    except Exception as e:
                        logger.error(f"[WX849] 读取图片文件失败: {e}")
                        return None

        logger.error(f"[WX849] 未找到图片文件: msg_id={msg_id}")
        return None

    def _process_voice_message(self, cmsg):
        """处理语音消息"""
        import xml.etree.ElementTree as ET
        import re
        
        cmsg.ctype = ContextType.VOICE
        
        # 保存原始内容，避免修改
        original_content = cmsg.content
        
        # 检查内容是否为XML格式
        is_xml_content = original_content.strip().startswith("<?xml") or original_content.strip().startswith("<msg")
        
        # 首先尝试从XML中提取发送者信息
        if is_xml_content:
            logger.debug(f"[WX849] 语音消息：尝试从XML提取发送者")
            try:
                # 使用正则表达式从XML字符串中提取fromusername属性或元素
                match = re.search(r'fromusername\s*=\s*["\'](.*?)["\']', original_content)
                if match:
                    cmsg.sender_wxid = match.group(1)
                    logger.debug(f"[WX849] 语音消息：从XML属性提取的发送者ID: {cmsg.sender_wxid}")
                else:
                    # 尝试从元素中提取
                    match = re.search(r'<fromusername>(.*?)</fromusername>', original_content)
                    if match:
                        cmsg.sender_wxid = match.group(1)
                        logger.debug(f"[WX849] 语音消息：从XML元素提取的发送者ID: {cmsg.sender_wxid}")
                    else:
                        logger.debug("[WX849] 语音消息：未找到fromusername")
                        
                        # 尝试使用ElementTree解析
                        try:
                            root = ET.fromstring(original_content)
                            # 尝试查找语音元素的fromusername属性
                            voice_element = root.find('voicemsg')
                            if voice_element is not None and 'fromusername' in voice_element.attrib:
                                cmsg.sender_wxid = voice_element.attrib['fromusername']
                                logger.debug(f"[WX849] 语音消息：使用ElementTree提取的发送者ID: {cmsg.sender_wxid}")
                        except Exception as e:
                            logger.debug(f"[WX849] 语音消息：使用ElementTree解析失败: {e}")
            except Exception as e:
                logger.debug(f"[WX849] 语音消息：提取发送者失败: {e}")
                
        # 如果无法从XML提取，再尝试传统的分割方法
        if not cmsg.sender_wxid and (cmsg.is_group or cmsg.from_user_id.endswith("@chatroom")):
            cmsg.is_group = True
            split_content = original_content.split(":\n", 1)
            if len(split_content) > 1:
                cmsg.sender_wxid = split_content[0]
                logger.debug(f"[WX849] 语音消息：使用分割方法提取的发送者ID: {cmsg.sender_wxid}")
            else:
                # 处理没有换行的情况
                split_content = original_content.split(":", 1)
                if len(split_content) > 1:
                    cmsg.sender_wxid = split_content[0]
                    logger.debug(f"[WX849] 语音消息：使用冒号分割提取的发送者ID: {cmsg.sender_wxid}")
        
        # 对于私聊消息，使用from_user_id作为发送者ID
        if not cmsg.sender_wxid and not cmsg.is_group:
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False
        
        # 设置actual_user_id和actual_user_nickname
        cmsg.actual_user_id = cmsg.sender_wxid or cmsg.from_user_id
        cmsg.actual_user_nickname = cmsg.sender_wxid or cmsg.from_user_id
        
        # 解析语音信息 (保留此功能以获取语音URL等信息)
        try:
            root = ET.fromstring(original_content)
            voice_element = root.find('voicemsg')
            if voice_element is not None:
                cmsg.voice_info = {
                    'voiceurl': voice_element.get('voiceurl'),
                    'length': voice_element.get('length')
                }
                logger.debug(f"解析语音XML成功: voiceurl={cmsg.voice_info['voiceurl']}, length={cmsg.voice_info['length']}")
        except Exception as e:
            logger.debug(f"解析语音消息失败: {e}, 内容: {original_content[:100]}")
            cmsg.voice_info = {}
            
        # 确保保留原始XML内容
        cmsg.content = original_content
        
        # 最终检查，确保发送者不是XML内容
        if not cmsg.sender_wxid or "<" in cmsg.sender_wxid:
            cmsg.sender_wxid = "未知发送者"
            cmsg.actual_user_id = cmsg.sender_wxid
            cmsg.actual_user_nickname = cmsg.sender_wxid
        
        # 输出日志，显示完整XML内容
        logger.info(f"收到语音消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid}\nXML内容: {cmsg.content}")

    def _process_video_message(self, cmsg):
        """处理视频消息"""
        import xml.etree.ElementTree as ET
        import re
        
        cmsg.ctype = ContextType.VIDEO
        
        # 保存原始内容，避免修改
        original_content = cmsg.content
        
        # 检查内容是否为XML格式
        is_xml_content = original_content.strip().startswith("<?xml") or original_content.strip().startswith("<msg")
        
        # 首先尝试从XML中提取发送者信息
        if is_xml_content:
            logger.debug(f"[WX849] 视频消息：尝试从XML提取发送者")
            try:
                # 使用正则表达式从XML字符串中提取fromusername属性或元素
                match = re.search(r'fromusername\s*=\s*["\'](.*?)["\']', original_content)
                if match:
                    cmsg.sender_wxid = match.group(1)
                    logger.debug(f"[WX849] 视频消息：从XML属性提取的发送者ID: {cmsg.sender_wxid}")
                else:
                    # 尝试从元素中提取
                    match = re.search(r'<fromusername>(.*?)</fromusername>', original_content)
                    if match:
                        cmsg.sender_wxid = match.group(1)
                        logger.debug(f"[WX849] 视频消息：从XML元素提取的发送者ID: {cmsg.sender_wxid}")
                    else:
                        logger.debug("[WX849] 视频消息：未找到fromusername")
                        
                        # 尝试使用ElementTree解析
                        try:
                            root = ET.fromstring(original_content)
                            # 尝试查找video元素的fromusername属性
                            video_element = root.find('videomsg')
                            if video_element is not None and 'fromusername' in video_element.attrib:
                                cmsg.sender_wxid = video_element.attrib['fromusername']
                                logger.debug(f"[WX849] 视频消息：使用ElementTree提取的发送者ID: {cmsg.sender_wxid}")
                        except Exception as e:
                            logger.debug(f"[WX849] 视频消息：使用ElementTree解析失败: {e}")
            except Exception as e:
                logger.debug(f"[WX849] 视频消息：提取发送者失败: {e}")
                
        # 如果无法从XML提取，再尝试传统的分割方法
        if not cmsg.sender_wxid and (cmsg.is_group or cmsg.from_user_id.endswith("@chatroom")):
            cmsg.is_group = True
            split_content = original_content.split(":\n", 1)
            if len(split_content) > 1:
                cmsg.sender_wxid = split_content[0]
                logger.debug(f"[WX849] 视频消息：使用分割方法提取的发送者ID: {cmsg.sender_wxid}")
            else:
                # 处理没有换行的情况
                split_content = original_content.split(":", 1)
                if len(split_content) > 1:
                    cmsg.sender_wxid = split_content[0]
                    logger.debug(f"[WX849] 视频消息：使用冒号分割提取的发送者ID: {cmsg.sender_wxid}")
        
        # 对于私聊消息，使用from_user_id作为发送者ID
        if not cmsg.sender_wxid and not cmsg.is_group:
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False
            
        # 设置actual_user_id和actual_user_nickname
        cmsg.actual_user_id = cmsg.sender_wxid or cmsg.from_user_id
        cmsg.actual_user_nickname = cmsg.sender_wxid or cmsg.from_user_id
            
        # 确保保留原始XML内容
        cmsg.content = original_content
        
        # 最终检查，确保发送者不是XML内容
        if not cmsg.sender_wxid or "<" in cmsg.sender_wxid:
            cmsg.sender_wxid = "未知发送者"
            cmsg.actual_user_id = cmsg.sender_wxid
            cmsg.actual_user_nickname = cmsg.sender_wxid
        
        # 输出日志，显示完整XML内容
        logger.info(f"收到视频消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid}\nXML内容: {cmsg.content}")

    def _process_emoji_message(self, cmsg):
        """处理表情消息"""
        import xml.etree.ElementTree as ET
        import re
        
        cmsg.ctype = ContextType.TEXT  # 表情消息通常也用TEXT类型
        
        # 保存原始内容，避免修改
        original_content = cmsg.content
        
        # 检查内容是否为XML格式
        is_xml_content = original_content.strip().startswith("<?xml") or original_content.strip().startswith("<msg")
        
        # 首先尝试从XML中提取发送者信息
        if is_xml_content:
            logger.debug(f"[WX849] 表情消息：尝试从XML提取发送者")
            try:
                # 使用正则表达式从XML中提取fromusername属性
                match = re.search(r'fromusername\s*=\s*["\'](.*?)["\']', original_content)
                if match:
                    cmsg.sender_wxid = match.group(1)
                    logger.debug(f"[WX849] 表情消息：从XML提取的发送者ID: {cmsg.sender_wxid}")
                else:
                    logger.debug("[WX849] 表情消息：未找到fromusername属性")
                    
                    # 尝试使用ElementTree解析
                    try:
                        root = ET.fromstring(original_content)
                        emoji_element = root.find('emoji')
                        if emoji_element is not None and 'fromusername' in emoji_element.attrib:
                            cmsg.sender_wxid = emoji_element.attrib['fromusername']
                            logger.debug(f"[WX849] 表情消息：使用ElementTree提取的发送者ID: {cmsg.sender_wxid}")
                    except Exception as e:
                        logger.debug(f"[WX849] 表情消息：使用ElementTree解析失败: {e}")
            except Exception as e:
                logger.debug(f"[WX849] 表情消息：提取发送者失败: {e}")
                
        # 如果无法从XML提取，再尝试传统的分割方法
        if not cmsg.sender_wxid and (cmsg.is_group or cmsg.from_user_id.endswith("@chatroom")):
            cmsg.is_group = True
            split_content = original_content.split(":\n", 1)
            if len(split_content) > 1:
                cmsg.sender_wxid = split_content[0]
                logger.debug(f"[WX849] 表情消息：使用分割方法提取的发送者ID: {cmsg.sender_wxid}")
            else:
                # 处理没有换行的情况
                split_content = original_content.split(":", 1)
                if len(split_content) > 1:
                    cmsg.sender_wxid = split_content[0]
                    logger.debug(f"[WX849] 表情消息：使用冒号分割提取的发送者ID: {cmsg.sender_wxid}")
        
        # 对于私聊消息，使用from_user_id作为发送者ID
        if not cmsg.sender_wxid and not cmsg.is_group:
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False
            
        # 设置actual_user_id和actual_user_nickname
        cmsg.actual_user_id = cmsg.sender_wxid or cmsg.from_user_id
        cmsg.actual_user_nickname = cmsg.sender_wxid or cmsg.from_user_id
            
        # 确保保留原始XML内容
        cmsg.content = original_content
        
        # 最终检查，确保发送者不是XML内容
        if not cmsg.sender_wxid or "<" in cmsg.sender_wxid:
            cmsg.sender_wxid = "未知发送者"
            cmsg.actual_user_id = cmsg.sender_wxid
            cmsg.actual_user_nickname = cmsg.sender_wxid
        
        # 输出日志，显示完整XML内容
        logger.info(f"收到表情消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid} \nXML内容: {cmsg.content}")

    def _process_xml_message(self, cmsg):
        """处理XML消息"""
        # 只保留re模块的导入
        import re
        import xml.etree.ElementTree as ET # <--- 确保导入 ET

        # 先默认设置为XML类型，添加错误处理
        try:
            cmsg.ctype = ContextType.XML
        except AttributeError:
            # 如果XML类型不存在，尝试添加它
            logger.error("[WX849] ContextType.XML 不存在，尝试动态添加")
            if not hasattr(ContextType, 'XML'):
                setattr(ContextType, 'XML', 'XML')
                logger.info("[WX849] 运行时添加 ContextType.XML 类型成功")
            try:
                cmsg.ctype = ContextType.XML
            except:
                # 如果仍然失败，回退到TEXT类型
                logger.error("[WX849] 设置 ContextType.XML 失败，回退到 TEXT 类型")
                cmsg.ctype = ContextType.TEXT
        
        # 添加调试日志，记录原始XML内容
        logger.debug(f"[WX849] 开始处理XML消息，消息ID: {cmsg.msg_id}, 内容长度: {len(cmsg.content)}")
        if cmsg.content and len(cmsg.content) > 0:
            logger.debug(f"[WX849] XML内容前100字符: {cmsg.content[:100]}")
        else:
            logger.debug(f"[WX849] XML内容为空")
        
        original_content = cmsg.content
        is_xml_content = original_content.strip().startswith("<?xml") or original_content.strip().startswith("<msg")

        # 尝试解析 appmsg 类型 5 (分享链接)
        if is_xml_content:
            try:
                root = ET.fromstring(original_content)
                appmsg_element = root.find("appmsg")
                if appmsg_element is not None:
                    app_type_element = appmsg_element.find("type")
                    app_type = -1
                    if app_type_element is not None and app_type_element.text is not None:
                        try:
                            app_type = int(app_type_element.text)
                        except ValueError:
                            logger.debug(f"[WX849] XML AppMsg type is not a valid integer: {app_type_element.text}")

                    if app_type == 5:  # Type 5: URL/分享链接
                        url_element = appmsg_element.find("url")
                        title_element = appmsg_element.find("title") # 可选：获取标题用于日志或调试
                        title = title_element.text if title_element is not None else "N/A"

                        if url_element is not None and url_element.text:
                            extracted_url = url_element.text.strip() # 使用 .strip() 清理可能的空白字符
                            if extracted_url.startswith("//"):
                                extracted_url = "http:" + extracted_url
                                logger.debug(f"[WX849] AppMsg Type 5 URL was scheme-relative, prepended http:. New URL: {extracted_url}")
                            
                            # 再次检查处理后的URL是否有效(可选，但推荐)
                            if not extracted_url or not (extracted_url.startswith("http://") or extracted_url.startswith("https://")):
                                logger.warning(f"[WX849] AppMsg Type 5 URL after processing is still invalid: {extracted_url}. Original: {url_element.text}")
                                # 如果URL处理后仍然无效，可以选择不设置SHARING类型或返回，避免后续插件出错
                                # 此处为了保持原逻辑，我们继续，但JinaSum仍可能因其他原因拒绝
                                cmsg.content = url_element.text # 保留原始URL以防万一或进行不同处理
                                # cmsg.ctype = ContextType.TEXT # 或者将其视为普通文本
                            else:
                                cmsg.content = extracted_url

                            cmsg.ctype = ContextType.SHARING
                            logger.info(f"[WX849] 成功转换XML AppMsg Type 5 (分享链接) 为 SHARING. URL: {cmsg.content}, Title: {title}")
                            return

                    # 可在此处添加对其他 app_type 的处理，例如文件(6)、小程序(33)等
                    # elif app_type == 6: # 文件
                    # ...
                    # return

            except ET.ParseError as xml_err:
                logger.warning(f"[WX849] 解析XML (用于appmsg type 5检测) 失败: {xml_err}, 内容: {original_content[:200]}")
            except Exception as e_parse:
                logger.error(f"[WX849] 处理XML (用于appmsg type 5检测) 时发生未知错误: {e_parse}, 内容: {original_content[:200]}")
        
        # 处理群聊/私聊消息发送者 (如果上面的 app_type 5 没有 return，则会执行这里)
        if cmsg.is_group or cmsg.from_user_id.endswith("@chatroom"):
            cmsg.is_group = True
            # 先默认设置一个空的sender_wxid
            cmsg.sender_wxid = ""
            
            # 尝试从XML中提取发送者信息
            if is_xml_content:
                logger.debug(f"[WX849] XML消息：尝试从XML提取发送者")
                try:
                    # 使用正则表达式从XML中提取fromusername属性
                    match = re.search(r'fromusername\s*=\s*["\'](.*?)["\']', original_content)
                    if match:
                        cmsg.sender_wxid = match.group(1)
                        logger.debug(f"[WX849] XML消息：从XML提取的发送者ID: {cmsg.sender_wxid}")
                    else:
                        # 尝试从元素中提取
                        match = re.search(r'<fromusername>(.*?)</fromusername>', original_content)
                        if match:
                            cmsg.sender_wxid = match.group(1)
                            logger.debug(f"[WX849] XML消息：从XML元素提取的发送者ID: {cmsg.sender_wxid}")
                        else:
                            logger.debug("[WX849] XML消息：未找到fromusername")
                except Exception as e:
                    logger.debug(f"[WX849] XML消息：提取发送者失败: {e}")
            
            # 如果无法从XML提取，尝试传统的分割方法
            if not cmsg.sender_wxid:
                split_content = original_content.split(":\n", 1)
                if len(split_content) > 1 and not split_content[0].startswith("<"):
                    cmsg.sender_wxid = split_content[0]
                    logger.debug(f"[WX849] XML消息：使用分割方法提取的发送者ID: {cmsg.sender_wxid}")
                else:
                    # 处理没有换行的情况
                    split_content = original_content.split(":", 1)
                    if len(split_content) > 1 and not split_content[0].startswith("<"):
                        cmsg.sender_wxid = split_content[0]
                        logger.debug(f"[WX849] XML消息：使用冒号分割提取的发送者ID: {cmsg.sender_wxid}")
            
            # 如果仍然无法提取，使用默认值
            if not cmsg.sender_wxid:
                cmsg.sender_wxid = f"未知用户_{cmsg.from_user_id}"
                logger.debug(f"[WX849] XML消息：使用默认发送者ID: {cmsg.sender_wxid}")
        else:
            # 私聊消息
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False
        
        # 设置actual_user_id和actual_user_nickname
        cmsg.actual_user_id = cmsg.sender_wxid or cmsg.from_user_id
        cmsg.actual_user_nickname = cmsg.sender_wxid or cmsg.from_user_id
        
        # 输出日志，显示完整XML内容
        logger.info(f"收到XML消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid}\nXML内容: {cmsg.content}")

    def _process_system_message(self, cmsg):
        """处理系统消息"""
        # 移除重复导入的ET
        
        # 检查是否是拍一拍消息
        if "<pat" in cmsg.content:
            try:
                root = ET.fromstring(cmsg.content)
                pat = root.find("pat")
                if pat is not None:
                    cmsg.ctype = ContextType.PAT  # 使用自定义类型
                    patter = pat.find("fromusername").text if pat.find("fromusername") is not None else ""
                    patted = pat.find("pattedusername").text if pat.find("pattedusername") is not None else ""
                    pat_suffix = pat.find("patsuffix").text if pat.find("patsuffix") is not None else ""
                    cmsg.pat_info = {
                        "patter": patter,
                        "patted": patted,
                        "suffix": pat_suffix
                    }
                    
                    # 设置actual_user_id和actual_user_nickname
                    cmsg.sender_wxid = patter
                    cmsg.actual_user_id = patter
                    cmsg.actual_user_nickname = patter
                    
                    # 日志输出
                    logger.info(f"收到拍一拍消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid} 拍者:{patter} 被拍:{patted} 后缀:{pat_suffix}")
                    return
            except Exception as e:
                logger.debug(f"[WX849] 解析拍一拍消息失败: {e}")
        
        # 如果不是特殊系统消息，按普通系统消息处理
        cmsg.ctype = ContextType.SYSTEM
        
        # 设置系统消息的actual_user_id和actual_user_nickname为系统
        cmsg.sender_wxid = "系统消息"
        cmsg.actual_user_id = "系统消息"
        cmsg.actual_user_nickname = "系统消息"
        
        logger.info(f"收到系统消息: ID:{cmsg.msg_id} 来自:{cmsg.from_user_id} 发送人:{cmsg.sender_wxid} 内容:{cmsg.content}")

    async def _call_api(self, endpoint, params, retry_count=0, max_retries=2):
        """调用API接口
        
        Args:
            endpoint (str): API端点，如 "/Login/GetQR"
            params (dict): API参数字典
            retry_count (int, optional): 当前重试次数. Defaults to 0.
            max_retries (int, optional): 最大重试次数. Defaults to 2.
            
        Returns:
            dict: API响应结果
        """
        try:
            import aiohttp
            
            # 获取API配置
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            api_port = conf().get("wx849_api_port", 9000)
            protocol_version = conf().get("wx849_protocol_version", "849")

            # 确定API路径前缀
            if protocol_version == "855" or protocol_version == "ipad":
                api_path_prefix = "/api"
            else:
                api_path_prefix = "/VXAPI"

            # 确保endpoint格式正确 - 标准化路径分隔符并确保开头有/
            if endpoint:
                # 替换反斜杠为正斜杠，确保跨平台兼容
                endpoint = endpoint.replace('\\', '/')
                # 确保开头有一个斜杠
                if not endpoint.startswith('/'):
                    endpoint = '/' + endpoint
            
            # 构建完整的API URL
            url = f"http://{api_host}:{api_port}{api_path_prefix}{endpoint}"
            
            # 记录详细的API调用信息
            logger.debug(f"[WX849] API调用: {url}")
            logger.debug(f"[WX849] 请求参数: {json.dumps(params, ensure_ascii=False)}")
            
            # 判断是否是需要使用表单数据的请求
            need_form_data = False
            form_endpoints = ["/Login/HeartBeat", "/Login/TwiceAutoAuth", "/Login/CheckQR", "/Login/GetCacheInfo"]
            for form_endpoint in form_endpoints:
                if endpoint.endswith(form_endpoint):
                    need_form_data = True
                    logger.debug(f"[WX849] 检测到需要使用表单数据的端点: {endpoint}")
                    break
                    
            # 添加详细的调试日志
            if need_form_data:
                logger.debug(f"[WX849] 使用表单数据提交")
                content_type = "application/x-www-form-urlencoded"
                # 将字典转换为表单格式
                if isinstance(params, dict):
                    import urllib.parse
                    form_data = {}
                    # 保留所有原始参数，但确保以小写和大写方式提供wxid
                    for key, value in params.items():
                        form_data[key] = value
                    
                    # 确保同时提供wxid和Wxid两种形式，增加兼容性
                    if "wxid" in params and "Wxid" not in params:
                        form_data["Wxid"] = params["wxid"]
                    elif "Wxid" in params and "wxid" not in params:
                        form_data["wxid"] = params["Wxid"]
                    
                    # 编码参数
                    data = urllib.parse.urlencode(form_data)
                    logger.debug(f"[WX849] 表单数据: {data}")
                else:
                    data = params
            else:
                logger.debug(f"[WX849] 使用JSON数据提交")
                content_type = "application/json"
                data = params
            
            # 发送请求，设置超时时间
            async with aiohttp.ClientSession() as session:
                headers = {"Content-Type": content_type}
                try:
                    # 根据内容类型选择不同的请求方式
                    if content_type == "application/x-www-form-urlencoded":
                        logger.debug(f"[WX849] 发送表单请求: {url}")
                        async with session.post(url, data=data, headers=headers, timeout=60) as response:
                            if response.status == 200:
                                # 读取响应内容
                                text = await response.text()
                                logger.debug(f"[WX849] 收到响应: {text}")
                                
                                try:
                                    # 尝试解析为JSON
                                    result = await response.json(content_type=None)
                                    logger.debug(f"[WX849] 解析为JSON: {json.dumps(result, ensure_ascii=False)}")
                                except Exception as json_err:
                                    logger.error(f"[WX849] JSON解析失败: {json_err}, 原始内容: {text}")
                                    # 返回错误响应
                                    return {"Success": False, "Message": f"JSON解析错误: {str(json_err)}", "RawResponse": text}
                                
                                # 检查是否有token过期问题
                                if retry_count < max_retries and isinstance(params, dict):
                                    wxid = params.get("wxid", params.get("Wxid", ""))
                                    device_id = params.get("device_id", params.get("DeviceId", ""))
                                    
                                    if wxid:
                                        processed_result = await self._process_api_response(result, wxid, device_id)
                                        
                                        # 如果需要重试（token刷新成功）
                                        if isinstance(processed_result, dict) and processed_result.get("__retry_needed__", False):
                                            logger.info(f"[WX849] 重试API请求: {endpoint}, 重试次数: {retry_count + 1}")
                                            # 递归调用，但增加重试计数
                                            return await self._call_api(endpoint, params, retry_count + 1, max_retries)
                                
                                return result
                            else:
                                # 处理非成功状态码
                                error_text = await response.text()
                                logger.error(f"[WX849] API请求失败: {response.status} - {error_text[:200]}")
                                return {"Success": False, "Message": f"HTTP错误 {response.status}", "ErrorDetail": error_text[:500]}
                    else:  # JSON格式
                        logger.debug(f"[WX849] 发送JSON请求: {url}")
                        async with session.post(url, json=data, headers=headers, timeout=60) as response:
                            if response.status == 200:
                                # 读取响应内容
                                text = await response.text()
                                logger.debug(f"[WX849] 收到响应: {text}")
                                
                                try:
                                    # 尝试解析为JSON
                                    result = await response.json(content_type=None)
                                    logger.debug(f"[WX849] 解析为JSON: {json.dumps(result, ensure_ascii=False)}")
                                except Exception as json_err:
                                    logger.error(f"[WX849] JSON解析失败: {json_err}, 原始内容: {text}")
                                    # 返回错误响应
                                    return {"Success": False, "Message": f"JSON解析错误: {str(json_err)}", "RawResponse": text}
                                
                                # 检查是否有token过期问题
                                if retry_count < max_retries and isinstance(params, dict):
                                    wxid = params.get("wxid", params.get("Wxid", ""))
                                    device_id = params.get("device_id", params.get("DeviceId", ""))
                                    
                                    if wxid:
                                        processed_result = await self._process_api_response(result, wxid, device_id)
                                        
                                        # 如果需要重试（token刷新成功）
                                        if isinstance(processed_result, dict) and processed_result.get("__retry_needed__", False):
                                            logger.info(f"[WX849] 重试API请求: {endpoint}, 重试次数: {retry_count + 1}")
                                            # 递归调用，但增加重试计数
                                            return await self._call_api(endpoint, params, retry_count + 1, max_retries)
                                
                                return result
                            else:
                                # 处理非成功状态码
                                error_text = await response.text()
                                logger.error(f"[WX849] API请求失败: {response.status} - {error_text[:200]}")
                                return {"Success": False, "Message": f"HTTP错误 {response.status}", "ErrorDetail": error_text[:500]}
                except aiohttp.ClientError as client_err:
                    # 客户端连接错误
                    logger.error(f"[WX849] HTTP请求错误: {client_err}")
                    return {"Success": False, "Message": f"HTTP请求错误: {str(client_err)}"}
                        
        except aiohttp.ClientError as e:
            # 处理连接错误
            logger.error(f"[WX849] API连接错误: {str(e)}")
            return {"Success": False, "Message": f"API连接错误: {str(e)}"}
        except asyncio.TimeoutError:
            # 处理超时错误
            logger.error(f"[WX849] API请求超时")
            return {"Success": False, "Message": "API请求超时"}
        except Exception as e:
            # 处理其他错误
            logger.error(f"[WX849] 调用API时出错: {str(e)}")
            import traceback
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return {"Success": False, "Message": f"API调用错误: {str(e)}"}

    async def _send_message(self, to_user_id, content, msg_type=1):
        """发送消息的异步方法"""
        try:
            # 移除ignore_protection参数，使用正确的API参数格式
            if not to_user_id:
                logger.error("[WX849] 发送消息失败: 接收者ID为空")
                return None
                
            # 根据API文档调整参数格式
            params = {
                "ToWxid": to_user_id,
                "Content": content,
                "Type": msg_type,
                "wxid": self.wxid,   # 发送者wxid参数名改为小写
                "At": ""             # 空字符串表示不@任何人
            }
            
            # 使用自定义的API调用方法
            result = await self._call_api("/Msg/SendTxt", params)
            
            # 检查结果
            if result and isinstance(result, dict):
                success = result.get("Success", False)
                if not success:
                    error_msg = result.get("Message", "未知错误")
                    logger.error(f"[WX849] 发送消息API返回错误: {error_msg}")
            
            return result
        except Exception as e:
            logger.error(f"[WX849] 发送消息失败: {e}")
            return None

    async def _send_image(self, to_user_id, image_source, context=None):
        """发送图片的异步方法，支持文件路径、BytesIO对象或BufferedReader对象""" # <--- 更新文档字符串
        try:
            image_base64 = None
            if isinstance(image_source, str):
                # 处理文件路径
                image_path = image_source
                # 检查文件是否存在
                if not os.path.exists(image_path):
                    logger.error(f"[WX849] 发送图片失败: 文件不存在 {image_path}")
                    return None
                # 读取图片文件并进行Base64编码
                with open(image_path, "rb") as f:
                    image_data = f.read()
                    image_base64 = base64.b64encode(image_data).decode('utf-8')
            elif isinstance(image_source, io.BytesIO):
                # 处理BytesIO对象
                image_data = image_source.getvalue()
                if not image_data:
                    logger.error("[WX849] 发送图片失败: BytesIO对象为空")
                    return None
                image_base64 = base64.b64encode(image_data).decode('utf-8')
            # --- 新增处理 BufferedReader 的分支 ---
            elif isinstance(image_source, io.BufferedReader):
                # 处理 BufferedReader 对象 - 改为获取路径并重新读取
                try:
                    image_path = image_source.name
                    if not image_path:
                        logger.error("[WX849] 发送图片失败: BufferedReader对象没有name属性")
                        return None
                    
                    # 确保文件仍然存在
                    if not os.path.exists(image_path):
                        logger.error(f"[WX849] 发送图片失败: 文件已被删除或不存在于路径 {image_path}")
                        return None
                        
                    # 重新打开文件读取
                    logger.debug(f"[WX849] 从BufferedReader获取路径并重新打开: {image_path}")
                    with open(image_path, "rb") as f:
                        image_data = f.read()
                        if not image_data:
                            logger.error(f"[WX849] 发送图片失败: 从路径 {image_path} 读取的数据为空")
                            return None
                        image_base64 = base64.b64encode(image_data).decode('utf-8')
                        
                except AttributeError:
                    logger.error("[WX849] 发送图片失败: 无法从BufferedReader对象获取name属性")
                    return None
                except FileNotFoundError:
                    logger.error(f"[WX849] 发送图片失败: 文件在重新打开时未找到 {image_path}")
                    return None
                except Exception as read_err:
                        logger.error(f"[WX849] 处理BufferedReader路径并读取文件时失败: {read_err}")
                        logger.error(traceback.format_exc()) # 添加traceback
                        return None
            # --- 结束新增分支 ---
            else:
                logger.error(f"[WX849] 发送图片失败: 不支持的图片源类型 {type(image_source)}")
                return None

            # --- 后续检查接收者ID和发送API的逻辑保持不变 ---
            # 检查接收者ID
            if not to_user_id:
                logger.error("[WX849] 发送图片失败: 接收者ID为空")
                return None

            # ... (省略后续未修改的代码) ...

            # 构建API参数 - 使用正确的参数格式
            params = {
                "ToWxid": to_user_id,
                "Base64": image_base64,
                "Wxid": self.wxid
            }

            # 调用API - 使用正确的API端点
            result = await self._call_api("/Msg/UploadImg", params)

            # ... (省略后续未修改的代码) ...
            return result
        except Exception as e:
            logger.error(f"[WX849] 发送图片失败: {e}")
            # 添加 traceback 方便调试
            logger.error(traceback.format_exc())
            return None

    async def _send_video(self, to_user_id, video_details):
        """发送视频的异步方法
        支持多种输入格式:
        1. 字典格式 {"video": 视频数据, "thumbnail": 封面图数据, "duration": 时长}
        2. 直接提供base64编码的视频和封面图
        """
        try:
            # 检查参数
            if not to_user_id:
                logger.error("[WX849] 发送视频失败: 接收者ID为空")
                return None

            # 支持两种输入格式：字典格式和直接的base64字符串
            video_base64 = None
            thumbnail_base64 = None
            video_duration = 10  # 默认10秒

            if isinstance(video_details, dict):
                # 处理字典格式输入
                video_input = video_details.get("video")
                thumbnail_input = video_details.get("thumbnail")
                video_duration = video_details.get("duration", 10)

                if not video_input:
                    logger.error(f"[WX849] 发送视频失败: 缺少必要的视频数据")
                    return None

                import base64
                import io
                import os

                # 处理视频数据
                if isinstance(video_input, str):
                    if video_input.startswith('data:video/mp4;base64,'):
                        # 已经是base64编码字符串，直接使用
                        video_base64 = video_input
                    elif os.path.exists(video_input):
                        # 视频文件路径
                        with open(video_input, "rb") as f:
                            video_data = f.read()
                            video_base64 = "data:video/mp4;base64," + base64.b64encode(video_data).decode('utf-8')
                    else:
                        # 尝试直接作为base64字符串使用
                        video_base64 = "data:video/mp4;base64," + video_input
                elif isinstance(video_input, io.BytesIO):
                    video_data = video_input.getvalue()
                    video_base64 = "data:video/mp4;base64," + base64.b64encode(video_data).decode('utf-8')
                elif isinstance(video_input, bytes):
                    video_data = video_input
                    video_base64 = "data:video/mp4;base64," + base64.b64encode(video_data).decode('utf-8')
                else:
                    logger.error(f"[WX849] 发送视频失败: 不支持的视频输入类型 {type(video_input)}")
                    return None

                # 处理封面图数据
                if thumbnail_input:
                    if isinstance(thumbnail_input, str):
                        if thumbnail_input.startswith('data:image/jpeg;base64,'):
                            # 已经是base64编码字符串，直接使用
                            thumbnail_base64 = thumbnail_input
                        elif os.path.exists(thumbnail_input):
                            # 图片文件路径
                            with open(thumbnail_input, "rb") as f:
                                thumbnail_data = f.read()
                                thumbnail_base64 = "data:image/jpeg;base64," + base64.b64encode(thumbnail_data).decode('utf-8')
                        else:
                            # 尝试直接作为base64字符串使用
                            thumbnail_base64 = "data:image/jpeg;base64," + thumbnail_input
                    elif isinstance(thumbnail_input, io.BytesIO):
                        thumbnail_data = thumbnail_input.getvalue()
                        thumbnail_base64 = "data:image/jpeg;base64," + base64.b64encode(thumbnail_data).decode('utf-8')
                    elif isinstance(thumbnail_input, bytes):
                        thumbnail_data = thumbnail_input
                        thumbnail_base64 = "data:image/jpeg;base64," + base64.b64encode(thumbnail_data).decode('utf-8')
                    else:
                        logger.error(f"[WX849] 封面图类型不支持: {type(thumbnail_input)}，将使用默认封面")
                
                # 验证时长参数
                if not isinstance(video_duration, (int, float)) or video_duration <= 0:
                    logger.warning(f"[WX849] 无效的视频时长: {video_duration}，使用默认值10秒")
                    video_duration = 10
            else:
                # 直接传入base64字符串
                video_base64 = video_details
                # 使用默认封面
                logger.debug("[WX849] 使用默认封面图片")

            # 处理视频base64前缀
            if video_base64 and not video_base64.startswith('data:video/mp4;base64,'):
                video_base64 = "data:video/mp4;base64," + video_base64

            # 设置默认封面(如果没有提供)
            if not thumbnail_base64 or thumbnail_base64 == "None":
                try:
                    # 使用1x1像素的透明PNG作为最小封面
                    thumbnail_base64 = "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                    logger.debug("[WX849] 使用内置1x1像素作为默认封面")
                except Exception as e:
                    logger.error(f"[WX849] 准备默认封面图片失败: {e}")
                    # 使用1x1像素的透明PNG作为最小封面
                    thumbnail_base64 = "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                    logger.debug("[WX849] 使用内置1x1像素作为默认封面")

            # 处理封面base64前缀
            if thumbnail_base64 and not thumbnail_base64.startswith('data:image/jpeg;base64,'):
                thumbnail_base64 = "data:image/jpeg;base64," + thumbnail_base64

            # 打印预估时间
            try:
                # 获取纯base64内容计算大小
                pure_video_base64 = video_base64
                if pure_video_base64.startswith("data:video/mp4;base64,"):
                    pure_video_base64 = pure_video_base64[len("data:video/mp4;base64,"):]

                # 计算文件大小 (KB)
                import base64
                file_len = len(base64.b64decode(pure_video_base64)) / 1024

                # 预估时间 (秒)，按300KB/s计算
                predict_time = int(file_len / 300)
                logger.info(f"[WX849] 开始发送视频: 预计{predict_time}秒, 视频大小:{file_len:.2f}KB, 时长:{video_duration}秒")
            except Exception as e:
                logger.debug(f"[WX849] 计算预估时间失败: {e}")

            # 构建API参数
            params = {
                "Wxid": self.wxid,
                "ToWxid": to_user_id,
                "Base64": video_base64,
                "ImageBase64": thumbnail_base64,
                "PlayLength": video_duration  # 必需参数，缺少会导致[Key:]数据不存在错误
            }

            # 调用API
            result = await self._call_api("/Msg/SendVideo", params)

            # 检查结果
            if result and isinstance(result, dict):
                success = result.get("Success", False)
                if success:
                    data = result.get("Data", {})
                    client_msg_id = data.get("clientMsgId")
                    new_msg_id = data.get("newMsgId")
                    logger.info(f"[WX849] 视频发送成功，返回ID: {client_msg_id}, {new_msg_id}")
                else:
                    error_msg = result.get("Message", "未知错误")
                    logger.error(f"[WX849] 视频API返回错误: {error_msg}")
                    logger.error(f"[WX849] 响应详情: {json.dumps(result, ensure_ascii=False)}")
            
            return result
        except Exception as e:
            logger.error(f"[WX849] 发送视频失败: {e}")
            import traceback
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None

    def send(self, reply: Reply, context: Context):
        """发送消息"""
        # 获取接收者ID
        receiver = context.get("receiver")
        if not receiver:
            # 如果context中没有接收者，尝试从消息对象中获取
            msg = context.get("msg")
            if msg and hasattr(msg, "from_user_id"):
                receiver = msg.from_user_id
        
        if not receiver:
            logger.error("[WX849] 发送消息失败: 无法确定接收者ID")
            return
            
        loop = asyncio.new_event_loop()
        
        if reply.type == ReplyType.TEXT:
            reply.content = remove_markdown_symbol(reply.content)
            result = loop.run_until_complete(self._send_message(receiver, reply.content))
            if result and isinstance(result, dict) and result.get("Success", False):
                logger.info(f"[WX849] 发送文本消息成功: 接收者: {receiver}")
                if conf().get("log_level", "INFO") == "DEBUG":
                    logger.debug(f"[WX849] 消息内容: {reply.content[:50]}...")
            else:
                logger.warning(f"[WX849] 发送文本消息可能失败: 接收者: {receiver}, 结果: {result}")
        
        elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
            reply.content = remove_markdown_symbol(reply.content)
            result = loop.run_until_complete(self._send_message(receiver, reply.content))
            if result and isinstance(result, dict) and result.get("Success", False):
                logger.info(f"[WX849] 发送消息成功: 接收者: {receiver}")
                if conf().get("log_level", "INFO") == "DEBUG":
                    logger.debug(f"[WX849] 消息内容: {reply.content[:50]}...")
            else:
                logger.warning(f"[WX849] 发送消息可能失败: 接收者: {receiver}, 结果: {result}")
        
        elif reply.type == ReplyType.IMAGE_URL:
            # 从网络下载图片并发送
            img_url = reply.content
            logger.debug(f"[WX849] 开始下载图片, url={img_url}")
            try:
                pic_res = requests.get(img_url, stream=True)
                # 使用临时文件保存图片
                tmp_path = os.path.join(get_appdata_dir(), f"tmp_img_{int(time.time())}.png")
                with open(tmp_path, 'wb') as f:
                    for block in pic_res.iter_content(1024):
                        f.write(block)
                
                # 使用我们的自定义方法发送图片
                result = loop.run_until_complete(self._send_image(receiver, tmp_path))
                
                if result and isinstance(result, dict) and result.get("Success", False):
                    logger.info(f"[WX849] 发送图片成功: 接收者: {receiver}")
                else:
                    logger.warning(f"[WX849] 发送图片可能失败: 接收者: {receiver}, 结果: {result}")
                
                # 删除临时文件
                try:
                    os.remove(tmp_path)
                except Exception as e:
                    logger.debug(f"[WX849] 删除临时图片文件失败: {e}")
            except Exception as e:
                logger.error(f"[WX849] 发送图片失败: {e}")
        
        elif reply.type == ReplyType.IMAGE: # 添加处理 ReplyType.IMAGE
            image_input = reply.content
            # 移除 os.path.exists 检查，交由 _send_image 处理
            # 使用我们的自定义方法发送本地图片或BytesIO
            result = loop.run_until_complete(self._send_image(receiver, image_input))
            
            if result and isinstance(result, dict) and result.get("Success", False):
                logger.info(f"[WX849] 发送图片成功: 接收者: {receiver}")
            else:
                logger.warning(f"[WX849] 发送图片可能失败: 接收者: {receiver}, 结果: {result}")
        
        elif reply.type == ReplyType.VIDEO:
            video_content = reply.content
            tmp_video_path = None
            tmp_thumb_path = None
            
            try:
                if isinstance(video_content, dict):
                    # 处理预先准备好的视频详情字典
                    logger.debug("[WX849] 发送预处理的视频数据")
                    result = loop.run_until_complete(self._send_video(receiver, video_content))
                
                elif isinstance(video_content, str) and cv2 is not None: # 处理视频URL
                    video_url = video_content
                    logger.info(f"[WX849] 收到视频URL，开始处理: {video_url}")
                    
                    # 确保 tmp 目录存在
                    tmp_dir = get_appdata_dir()
                    if not os.path.exists(tmp_dir):
                        os.makedirs(tmp_dir)
                    
                    # 1. 下载视频
                    ts = int(time.time())
                    tmp_video_path = os.path.join(tmp_dir, f"tmp_video_{ts}.mp4")
                    logger.debug(f"[WX849] 下载视频到: {tmp_video_path}")
                    
                    try:
                        res = requests.get(video_url, stream=True, timeout=60) # 增加超时
                        res.raise_for_status() # 检查HTTP错误
                        with open(tmp_video_path, 'wb') as f:
                            for chunk in res.iter_content(chunk_size=8192):
                                f.write(chunk)
                        logger.debug(f"[WX849] 视频下载完成")
                    except requests.exceptions.RequestException as e:
                        logger.error(f"[WX849] 下载视频失败: {e}")
                        return
                    except Exception as e:
                        logger.error(f"[WX849] 保存视频文件失败: {e}")
                        return

                    # 2. 使用 OpenCV 获取时长和缩略图
                    logger.debug(f"[WX849] 使用OpenCV处理视频: {tmp_video_path}")
                    cap = cv2.VideoCapture(tmp_video_path)
                    if not cap.isOpened():
                        logger.error(f"[WX849] 无法打开视频文件: {tmp_video_path}")
                        return

                    # 获取时长
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    duration = math.ceil(frame_count / fps) if fps > 0 else 0
                    logger.debug(f"[WX849] 视频信息 - FPS: {fps}, 帧数: {frame_count}, 时长: {duration}s")
                    
                    # 获取缩略图 (第一帧)
                    success, frame = cap.read()
                    cap.release() # 及时释放资源
                    
                    if not success:
                        logger.error(f"[WX849] 无法读取视频帧用于缩略图")
                        return
                        
                    tmp_thumb_path = os.path.join(tmp_dir, f"tmp_thumb_{ts}.jpg")
                    logger.debug(f"[WX849] 保存缩略图到: {tmp_thumb_path}")
                    if not cv2.imwrite(tmp_thumb_path, frame):
                       logger.error(f"[WX849] 保存缩略图失败: {tmp_thumb_path}")
                       return

                    # 3. 构建 video_details 并发送
                    video_details = {
                        "video": tmp_video_path,
                        "thumbnail": tmp_thumb_path,
                        "duration": duration
                    }
                    logger.debug(f"[WX849] 准备发送处理后的视频")
                    result = loop.run_until_complete(self._send_video(receiver, video_details))
                    
                elif isinstance(video_content, str) and cv2 is None:
                    logger.error("[WX849] 收到视频URL，但未安装opencv-python，无法处理")
                    result = None # 标记为失败
                
                else:
                    logger.error(f"[WX849] 发送视频失败: 不支持的 reply.content 类型: {type(video_content)}")
                    result = None # 标记为失败
                    
                # 日志记录发送结果
                if result and isinstance(result, dict) and result.get("Success", False):
                    logger.info(f"[WX849] 发送视频成功: 接收者: {receiver}")
                else:
                    # 增加详细的失败原因日志
                    reason = "处理失败或API返回错误"
                    if isinstance(video_content, str) and cv2 is None:
                        reason = "未安装opencv-python"
                    elif not isinstance(video_content, (dict, str)):
                        reason = f"不支持的内容类型 {type(video_content)}"
                    logger.warning(f"[WX849] 发送视频可能失败: 接收者: {receiver}, 原因: {reason}, API结果: {result}")

            except Exception as e:
                logger.error(f"[WX849] 处理并发送视频时发生意外错误: {e}")
                import traceback
                logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            finally:
                # 清理临时文件
                if tmp_video_path and os.path.exists(tmp_video_path):
                    try:
                        os.remove(tmp_video_path)
                        logger.debug(f"[WX849] 已删除临时视频文件: {tmp_video_path}")
                    except Exception as e:
                        logger.error(f"[WX849] 删除临时视频文件失败: {e}")
                if tmp_thumb_path and os.path.exists(tmp_thumb_path):
                    try:
                        os.remove(tmp_thumb_path)
                        logger.debug(f"[WX849] 已删除临时缩略图文件: {tmp_thumb_path}")
                    except Exception as e:
                        logger.error(f"[WX849] 删除临时缩略图文件失败: {e}")
        
        elif reply.type == ReplyType.APP:
            xml_content = reply.content
            logger.info(f"[WX849] APP message raw content type: {type(xml_content)}, content length: {len(xml_content)}")
            if conf().get("log_level", "INFO") == "DEBUG":
                 logger.debug(f"[WX849] APP XML Content: {xml_content[:500]}") # Log more content for debugging

            if not isinstance(xml_content, str):
                logger.error(f"[WX849] send app message failed: content must be XML string, got type={type(xml_content)}")
                return
            if not xml_content.strip():
                logger.error("[WX849] send app message failed: content is empty string")
                return
            
            # Extract app_type from XML content
            app_type = 3 # Default to 3 (music type from log example) if not found
            try:
                # Using regex to find <type>integer_value</type>
                match = re.search(r"<type>\s*(\d+)\s*</type>", xml_content, re.IGNORECASE)
                if match:
                    app_type = int(match.group(1))
                    logger.info(f"[WX849] Extracted app_type from XML: {app_type}")
                else:
                    logger.warning(f"[WX849] Could not find <type> tag in XML, using default app_type: {app_type}. XML: {xml_content[:300]}...")
            except Exception as e_parse_type:
                logger.error(f"[WX849] Error parsing app_type from XML: {e_parse_type}, using default: {app_type}. XML: {xml_content[:300]}...")
            
            result = loop.run_until_complete(self._send_app_xml(receiver, xml_content, app_type))
            if result and isinstance(result, dict) and result.get("Success", False):
                logger.info(f"[WX849] 发送App XML消息成功: 接收者: {receiver}, Type: {app_type}")
            else:
                logger.warning(f"[WX849] 发送App XML消息可能失败: 接收者: {receiver}, Type: {app_type}, 结果: {result}")

        # 移除不存在的ReplyType.Emoji类型处理
        # elif reply.type == ReplyType.Emoji:
        #     emoji_input = reply.content
        #     # 移除 os.path.exists 检查，交由 _send_emoji 处理
        #     # 使用我们的自定义方法发送表情
        #     result = loop.run_until_complete(self._send_emoji(receiver, emoji_input))
        #     
        #     if result and isinstance(result, dict) and result.get("Success", False):
        #         logger.info(f"[WX849] 发送表情成功: 接收者: {receiver}")
        #     else:
        #         logger.warning(f"[WX849] 发送表情可能失败: 接收者: {receiver}, 结果: {result}")
        
        # 移除不存在的ReplyType.App类型，改用ReplyType.MINIAPP
        elif reply.type == ReplyType.MINIAPP:
            app_input = reply.content
            # 移除 os.path.exists 检查，交由 _send_app 处理
            # 使用我们的自定义方法发送小程序
            result = loop.run_until_complete(self._send_app(receiver, app_input))
            
            if result and isinstance(result, dict) and result.get("Success", False):
                logger.info(f"[WX849] 发送小程序成功: 接收者: {receiver}")
            else:
                logger.warning(f"[WX849] 发送小程序可能失败: 接收者: {receiver}, 结果: {result}")
        
        # 移除不存在的ReplyType.System类型，使用ReplyType.INFO或忽略
        elif reply.type == ReplyType.INFO:
            system_input = reply.content
            # 移除 os.path.exists 检查，交由 _send_system 处理
            # 使用我们的自定义方法发送系统消息
            result = loop.run_until_complete(self._send_message(receiver, system_input))
            
            if result and isinstance(result, dict) and result.get("Success", False):
                logger.info(f"[WX849] 发送系统消息成功: 接收者: {receiver}")
            else:
                logger.warning(f"[WX849] 发送系统消息可能失败: 接收者: {receiver}, 结果: {result}")
        
        elif reply.type == ReplyType.VIDEO_URL:
            # 从网络下载视频URL并发送
            video_url = reply.content
            logger.info(f"[WX849] 开始处理视频URL: {video_url}")
            tmp_video_path = None
            tmp_thumb_path = None
            
            try:
                # 确保opencv-python已安装
                if cv2 is None:
                    logger.error("[WX849] 无法处理视频URL: 未安装opencv-python库")
                    # 发送错误消息给用户，通知安装依赖
                    error_msg = "抱歉，服务器未安装视频处理库(OpenCV)，无法发送视频。请联系管理员安装 opencv-python 包。"
                    loop.run_until_complete(self._send_message(receiver, error_msg))
                    return
                
                # 确保tmp目录存在
                tmp_dir = get_appdata_dir()
                if not os.path.exists(tmp_dir):
                    os.makedirs(tmp_dir)
                
                # 1. 下载视频
                ts = int(time.time())
                tmp_video_path = os.path.join(tmp_dir, f"tmp_video_{ts}.mp4")
                logger.debug(f"[WX849] 下载视频到: {tmp_video_path}")
                
                try:
                    res = requests.get(video_url, stream=True, timeout=60)
                    res.raise_for_status()
                    with open(tmp_video_path, 'wb') as f:
                        for chunk in res.iter_content(chunk_size=8192):
                            f.write(chunk)
                    logger.debug(f"[WX849] 视频下载完成")
                except requests.exceptions.RequestException as e:
                    logger.error(f"[WX849] 下载视频失败: {e}")
                    error_msg = f"视频下载失败: {str(e)}"
                    loop.run_until_complete(self._send_message(receiver, error_msg))
                    return
                
                # 2. 使用OpenCV获取时长和缩略图
                logger.debug(f"[WX849] 处理视频获取缩略图: {tmp_video_path}")
                cap = cv2.VideoCapture(tmp_video_path)
                if not cap.isOpened():
                    logger.error(f"[WX849] 无法打开视频文件: {tmp_video_path}")
                    error_msg = "视频处理失败: 无法打开视频文件"
                    loop.run_until_complete(self._send_message(receiver, error_msg))
                    return
                
                # 获取视频时长
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                duration = math.ceil(frame_count / fps) if fps > 0 else 10
                logger.debug(f"[WX849] 视频信息 - FPS: {fps}, 帧数: {frame_count}, 时长: {duration}秒")
                
                # 获取缩略图 (第一帧)
                success, frame = cap.read()
                cap.release()
                
                if not success:
                    logger.error(f"[WX849] 无法读取视频帧用于缩略图")
                    # 使用默认缩略图
                    import base64
                    thumbnail_base64 = "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                    logger.debug("[WX849] 使用默认缩略图")
                else:
                    # 保存缩略图 - 保持原始尺寸
                    tmp_thumb_path = os.path.join(tmp_dir, f"tmp_thumb_{ts}.jpg")
                    # 不调整尺寸，直接保存原始帧
                    if not cv2.imwrite(tmp_thumb_path, frame):
                        logger.error(f"[WX849] 保存缩略图失败")
                        # 使用默认缩略图
                        import base64
                        thumbnail_base64 = "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                        logger.debug("[WX849] 使用默认缩略图")
                    else:
                        # 记录缩略图尺寸信息
                        height, width, _ = frame.shape
                        logger.debug(f"[WX849] 缩略图尺寸: {width}x{height}")
                        thumbnail_base64 = None  # 使用文件路径
                
                # 3. 构建视频详情并发送
                video_details = {
                    "video": tmp_video_path,
                    "thumbnail": tmp_thumb_path if tmp_thumb_path and os.path.exists(tmp_thumb_path) else thumbnail_base64,
                    "duration": duration
                }
                logger.debug(f"[WX849] 准备发送视频: {video_details}")
                result = loop.run_until_complete(self._send_video(receiver, video_details))
                
                if result and isinstance(result, dict) and result.get("Success", False):
                    logger.info(f"[WX849] 视频发送成功: 接收者: {receiver}")
                else:
                    logger.warning(f"[WX849] 视频发送可能失败: 接收者: {receiver}, 结果: {result}")
                    # 尝试发送链接作为备用方案
                    fallback_msg = f"视频发送失败，您可以通过以下链接查看视频: {video_url}"
                    loop.run_until_complete(self._send_message(receiver, fallback_msg))
            except Exception as e:
                logger.error(f"[WX849] 处理视频URL失败: {e}")
                import traceback
                logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
                # 发送错误消息
                error_msg = f"视频处理失败: {str(e)}"
                try:
                    loop.run_until_complete(self._send_message(receiver, error_msg))
                    # 尝试发送链接作为备用方案
                    fallback_msg = f"您可以通过以下链接查看视频: {video_url}"
                    loop.run_until_complete(self._send_message(receiver, fallback_msg))
                except:
                    pass
            finally:
                # 清理临时文件
                if tmp_video_path and os.path.exists(tmp_video_path):
                    try:
                        os.remove(tmp_video_path)
                        logger.debug(f"[WX849] 已删除临时视频文件: {tmp_video_path}")
                    except Exception as e:
                        logger.error(f"[WX849] 删除临时视频文件失败: {e}")
                if tmp_thumb_path and os.path.exists(tmp_thumb_path):
                    try:
                        os.remove(tmp_thumb_path)
                        logger.debug(f"[WX849] 已删除临时缩略图文件: {tmp_thumb_path}")
                    except Exception as e:
                        logger.error(f"[WX849] 删除临时缩略图文件失败: {e}")
        
        elif reply.type == ReplyType.VOICE:
            original_voice_file_path = reply.content
            if not original_voice_file_path or not os.path.exists(original_voice_file_path):
                logger.error(f"[WX849] Send voice failed: Original voice file not found or path is empty: {original_voice_file_path}")
                return
            
            if not original_voice_file_path.lower().endswith('.mp3'):
                logger.error(f"[WX849] Send voice failed: Only .mp3 voice files are supported, got {original_voice_file_path}")
                return

            # FFmpeg preprocessing
            ffmpeg_path = _find_ffmpeg_path()
            
            # Correctly create temporary directory for ffmpeg output
            base_tmp_root = TmpDir().path() # e.g., ./tmp/
            voice_subdir_name = "wx849_voice"
            voice_tmp_dir = os.path.join(base_tmp_root, voice_subdir_name) # e.g., ./tmp/wx849_voice
            os.makedirs(voice_tmp_dir, exist_ok=True)
            processed_voice_path = os.path.join(voice_tmp_dir, f"ffmpeg_processed_{os.path.basename(original_voice_file_path)}")
            
            effective_voice_path = original_voice_file_path # Default to original if ffmpeg fails
            ffmpeg_success = False

            try:
                cmd = [
                    ffmpeg_path, "-y", "-i", original_voice_file_path,
                    "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k",
                    "-ac", "2", processed_voice_path
                ]
                logger.info(f"[WX849] Attempting to preprocess voice file with ffmpeg: {' '.join(cmd)}")
                process_result = subprocess.run(cmd, capture_output=True, text=True, check=False) # check=False to inspect manually
                if process_result.returncode == 0 and os.path.exists(processed_voice_path):
                    logger.info(f"[WX849] ffmpeg preprocessing successful. Using processed file: {processed_voice_path}")
                    effective_voice_path = processed_voice_path
                    ffmpeg_success = True
                else:
                    logger.warning(f"[WX849] ffmpeg preprocessing failed. Return code: {process_result.returncode}. Error: {process_result.stderr}. Will use original file.")
            except Exception as e_ffmpeg:
                logger.error(f"[WX849] Exception during ffmpeg preprocessing: {e_ffmpeg}. Will use original file.")

            temp_files_to_clean = []
            if ffmpeg_success and effective_voice_path != original_voice_file_path:
                temp_files_to_clean.append(effective_voice_path) # Add ffmpeg processed file for cleanup

            try:
                # Reduce segment duration to 25 seconds to see if it helps with EndFlag issue
                _total_duration_ms, segment_paths = split_audio(effective_voice_path, 20 * 1000) 
                temp_files_to_clean.extend(segment_paths) # Add segment paths from split_audio for cleanup

                if not segment_paths:
                    logger.error(f"[WX849] Voice splitting failed for {effective_voice_path}. No segments created.")
                    logger.info(f"[WX849] Attempting to send {effective_voice_path} as fallback.")
                    # Duration calculation for fallback is now inside _send_voice, so just pass path
                    fallback_result = loop.run_until_complete(self._send_voice(receiver, effective_voice_path))
                    if fallback_result and isinstance(fallback_result, dict) and fallback_result.get("Success", False):
                        logger.info(f"[WX849] Fallback: Sent voice file successfully: {effective_voice_path}")
                    else:
                        logger.warning(f"[WX849] Fallback: Sending voice file failed: {effective_voice_path}, Result: {fallback_result}")
                    return
                
                logger.info(f"[WX849] Voice file {effective_voice_path} split into {len(segment_paths)} segments.")

                for i, segment_path in enumerate(segment_paths):
                    # Duration calculation and SILK conversion are now inside _send_voice
                    segment_result = loop.run_until_complete(self._send_voice(receiver, segment_path))
                    if segment_result and isinstance(segment_result, dict) and segment_result.get("Success", False):
                        logger.info(f"[WX849] Sent voice segment {i+1}/{len(segment_paths)} successfully: {segment_path}")
                    else:
                        logger.warning(f"[WX849] Sending voice segment {i+1}/{len(segment_paths)} failed: {segment_path}, Result: {segment_result}")
                        # If a segment fails, we might decide to stop or continue. For now, continue.
                    
                    if i < len(segment_paths) - 1:
                        time.sleep(0.5)
            
            except Exception as e_split_send:
                logger.error(f"[WX849] Error during voice splitting or segmented sending for {effective_voice_path}: {e_split_send}")
                import traceback
                logger.error(traceback.format_exc())
            finally:
                logger.debug(f"[WX849] Cleaning up {len(temp_files_to_clean)} temporary voice file(s)...")
                for temp_file_path in temp_files_to_clean:
                    try:
                        if os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                            logger.debug(f"[WX849] Removed temporary voice file: {temp_file_path}")
                    except Exception as e_cleanup:
                        logger.warning(f"[WX849] Failed to remove temporary voice file {temp_file_path}: {e_cleanup}")

        else:
            logger.warning(f"[WX849] 不支持的回复类型: {reply.type}")
        
        loop.close() 

    async def _get_group_member_details(self, group_id):
        """获取群成员详情"""
        try:
            logger.debug(f"[WX849] 尝试获取群 {group_id} 的成员详情")
            
            # 检查是否已存在群成员信息，并检查是否需要更新
            # 定义群聊信息文件路径
            tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp")
            if not os.path.exists(tmp_dir):
                os.makedirs(tmp_dir)
            
            chatrooms_file = os.path.join(tmp_dir, 'wx849_rooms.json')
            
            # 读取现有的群聊信息（如果存在）
            chatrooms_info = {}
            if os.path.exists(chatrooms_file):
                try:
                    with open(chatrooms_file, 'r', encoding='utf-8') as f:
                        chatrooms_info = json.load(f)
                    logger.debug(f"[WX849] 已加载 {len(chatrooms_info)} 个现有群聊信息")
                except Exception as e:
                    logger.error(f"[WX849] 加载现有群聊信息失败: {str(e)}")
            
            # 检查该群聊是否已存在且成员信息是否已更新
            # 设定缓存有效期为24小时(86400秒)
            cache_expiry = 86400
            current_time = int(time.time())
            
            if (group_id in chatrooms_info and 
                "members" in chatrooms_info[group_id] and 
                len(chatrooms_info[group_id]["members"]) > 0 and
                "last_update" in chatrooms_info[group_id] and
                current_time - chatrooms_info[group_id]["last_update"] < cache_expiry):
                logger.debug(f"[WX849] 群 {group_id} 成员信息已存在且未过期，跳过更新")
                return chatrooms_info[group_id]
            
            logger.debug(f"[WX849] 群 {group_id} 成员信息不存在或已过期，开始更新")
            
            # ============== 新增：首先调用GetChatRoomInfo获取群名称 ==============
            # 调用API获取群详情
            info_params = {
                "QID": group_id,  # 群ID参数
                "wxid": self.wxid  # 自己的wxid参数，改为小写
            }
            
            # 获取API配置
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            api_port = conf().get("wx849_api_port", 9000)
            protocol_version = conf().get("wx849_protocol_version", "849")
            
            # 确定API路径前缀
            if protocol_version == "855" or protocol_version == "ipad":
                api_path_prefix = "/api"
            else:
                api_path_prefix = "/VXAPI"
            
            logger.info(f"[WX849] 正在请求群详情API: http://{api_host}:{api_port}{api_path_prefix}/Group/GetChatRoomInfo")
            logger.info(f"[WX849] 群详情请求参数: {json.dumps(info_params, ensure_ascii=False)}")
            
            # 调用GetChatRoomInfo API
            group_info_response = await self._call_api("/Group/GetChatRoomInfo", info_params)
            
            # 解析群名称
            group_name = None
            if group_info_response and isinstance(group_info_response, dict) and group_info_response.get("Success", False):
                data = group_info_response.get("Data", {})
                
                # 递归函数用于查找特定key的值
                def find_value(obj, key):
                    # 如果是字典
                    if isinstance(obj, dict):
                        # 直接检查当前字典
                        if key in obj:
                            return obj[key]
                        # 检查带有"string"嵌套的字典
                        if key in obj and isinstance(obj[key], dict) and "string" in obj[key]:
                            return obj[key]["string"]
                        # 递归检查字典的所有值
                        for k, v in obj.items():
                            result = find_value(v, key)
                            if result is not None:
                                return result
                    # 如果是列表
                    elif isinstance(obj, list):
                        # 递归检查列表的所有项
                        for item in obj:
                            result = find_value(item, key)
                            if result is not None:
                                return result
                    return None
                
                # 尝试多种可能的群名称字段
                for name_key in ["NickName", "ChatRoomName", "nickname", "chatroomname", "DisplayName", "displayname"]:
                    name_value = find_value(data, name_key)
                    if name_value:
                        if isinstance(name_value, dict) and "string" in name_value:
                            group_name = name_value["string"]
                        elif isinstance(name_value, str):
                            group_name = name_value
                        if group_name:
                            logger.info(f"[WX849] 成功获取到群名称: {group_name} (字段: {name_key})")
                            break
                
                # 如果找不到，记录整个响应以便调试
                if not group_name:
                    logger.warning(f"[WX849] 无法从API响应中提取群名称，响应内容: {json.dumps(data, ensure_ascii=False)[:200]}...")
            else:
                logger.warning(f"[WX849] 获取群详情失败: {group_info_response}")
            
            # 确保在chatrooms_info中创建该群的条目
            if group_id not in chatrooms_info:
                chatrooms_info[group_id] = {
                    "chatroomId": group_id,
                    "nickName": group_name or group_id,  # 如果获取到群名则使用，否则使用群ID
                    "chatRoomOwner": "",
                    "members": [],
                    "last_update": int(time.time())
                }
            else:
                # 更新现有条目的群名称
                if group_name:
                    chatrooms_info[group_id]["nickName"] = group_name
            
            # 立即保存群名称信息
            with open(chatrooms_file, 'w', encoding='utf-8') as f:
                json.dump(chatrooms_info, f, ensure_ascii=False, indent=2)
            
            logger.info(f"[WX849] 已更新群 {group_id} 的名称: {group_name or '未获取到'}")
            
            # 更新群名缓存
            if group_name:
                if not hasattr(self, "group_name_cache"):
                    self.group_name_cache = {}
                self.group_name_cache[f"group_name_{group_id}"] = group_name
            # ============== 群名称获取完毕 ==============
            
            # 接下来继续获取群成员详情
            # 调用API获取群成员详情
            params = {
                "QID": group_id,  # 群ID参数
                "wxid": self.wxid  # 自己的wxid参数，改为小写
            }
            
            try:
                # 构建完整的API URL用于日志
                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Group/GetChatRoomMemberDetail"
                logger.debug(f"[WX849] 正在请求群成员详情API: {api_url}")
                logger.debug(f"[WX849] 请求参数: {json.dumps(params, ensure_ascii=False)}")
                
                # 调用API获取群成员详情
                response = await self._call_api("/Group/GetChatRoomMemberDetail", params)
                
                if not response or not isinstance(response, dict):
                    logger.error(f"[WX849] 获取群成员详情失败: 无效响应")
                    return None
                
                # 检查响应是否成功
                if not response.get("Success", False):
                    logger.error(f"[WX849] 获取群成员详情失败: {response.get('Message', '未知错误')}")
                    return None
                
                # 提取NewChatroomData
                data = response.get("Data", {})
                new_chatroom_data = data.get("NewChatroomData", {})
                
                if not new_chatroom_data:
                    logger.error(f"[WX849] 获取群成员详情失败: 响应中无NewChatroomData")
                    return None
                
                # 提取成员信息
                member_count = new_chatroom_data.get("MemberCount", 0)
                chat_room_members = new_chatroom_data.get("ChatRoomMember", [])
                
                # 确保是有效的成员列表
                if not isinstance(chat_room_members, list):
                    logger.error(f"[WX849] 获取群成员详情失败: ChatRoomMember不是有效的列表")
                    return None
                
                # 更新群聊成员信息
                members = []
                for member in chat_room_members:
                    if not isinstance(member, dict):
                        continue
                    
                    # 提取成员必要信息
                    member_info = {
                        "UserName": member.get("UserName", ""),
                        "NickName": member.get("NickName", ""),
                        "DisplayName": member.get("DisplayName", ""),
                        "ChatroomMemberFlag": member.get("ChatroomMemberFlag", 0),
                        "InviterUserName": member.get("InviterUserName", ""),
                        "BigHeadImgUrl": member.get("BigHeadImgUrl", ""),
                        "SmallHeadImgUrl": member.get("SmallHeadImgUrl", "")
                    }
                    
                    members.append(member_info)
                
                # 更新群聊信息
                chatrooms_info[group_id]["members"] = members
                chatrooms_info[group_id]["last_update"] = int(time.time())
                chatrooms_info[group_id]["memberCount"] = member_count
                
                # 同时更新群主信息
                for member in members:
                    if member.get("ChatroomMemberFlag") == 2049:  # 群主标志
                        chatrooms_info[group_id]["chatRoomOwner"] = member.get("UserName", "")
                        break
                
                # 保存到文件
                with open(chatrooms_file, 'w', encoding='utf-8') as f:
                    json.dump(chatrooms_info, f, ensure_ascii=False, indent=2)
                
                logger.info(f"[WX849] 已更新群聊 {group_id} 成员信息，成员数: {len(members)}")
                
                # 返回成员信息
                return new_chatroom_data
            except Exception as e:
                logger.error(f"[WX849] 获取群成员详情失败: {e}")
                logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
                return None
        except Exception as e:
            logger.error(f"[WX849] 获取群成员详情过程中出错: {e}")
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None

    async def _get_group_name(self, group_id):
        """获取群名称"""
        try:
            logger.debug(f"[WX849] 尝试获取群 {group_id} 的名称")
            
            # 检查缓存中是否有群名
            cache_key = f"group_name_{group_id}"
            if hasattr(self, "group_name_cache") and cache_key in self.group_name_cache:
                cached_name = self.group_name_cache[cache_key]
                logger.debug(f"[WX849] 从缓存中获取群名: {cached_name}")
                
                # 检查是否需要更新群成员详情
                tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp")
                chatrooms_file = os.path.join(tmp_dir, 'wx849_rooms.json')
                
                need_update = True
                # 设定缓存有效期为24小时(86400秒)
                cache_expiry = 86400
                current_time = int(time.time())
                
                if os.path.exists(chatrooms_file):
                    try:
                        with open(chatrooms_file, 'r', encoding='utf-8') as f:
                            chatrooms_info = json.load(f)
                        
                        # 检查群信息是否存在且未过期
                        if (group_id in chatrooms_info and 
                            "last_update" in chatrooms_info[group_id] and 
                            current_time - chatrooms_info[group_id]["last_update"] < cache_expiry and
                            "members" in chatrooms_info[group_id] and 
                            len(chatrooms_info[group_id]["members"]) > 0):
                            logger.debug(f"[WX849] 群 {group_id} 信息已存在且未过期，跳过更新")
                            need_update = False
                    except Exception as e:
                        logger.error(f"[WX849] 检查群信息缓存时出错: {e}")
                
                # 只有需要更新时才启动线程获取群成员详情
                if need_update:
                    logger.debug(f"[WX849] 群 {group_id} 信息需要更新，启动更新线程")
                    threading.Thread(target=lambda: asyncio.run(self._get_group_member_details(group_id))).start()
                
                return cached_name
            
            # 检查文件中是否已经有群信息，且未过期
            tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp")
            if not os.path.exists(tmp_dir):
                os.makedirs(tmp_dir)
            
            chatrooms_file = os.path.join(tmp_dir, 'wx849_rooms.json')
            
            # 设定缓存有效期为24小时(86400秒)
            cache_expiry = 86400
            current_time = int(time.time())
            
            if os.path.exists(chatrooms_file):
                try:
                    with open(chatrooms_file, 'r', encoding='utf-8') as f:
                        chatrooms_info = json.load(f)
                    
                    # 检查群信息是否存在且未过期
                    if (group_id in chatrooms_info and 
                        "nickName" in chatrooms_info[group_id] and
                        chatrooms_info[group_id]["nickName"] and
                        chatrooms_info[group_id]["nickName"] != group_id and
                        "last_update" in chatrooms_info[group_id] and 
                        current_time - chatrooms_info[group_id]["last_update"] < cache_expiry):
                        
                        # 从文件中获取群名
                        group_name = chatrooms_info[group_id]["nickName"]
                        logger.debug(f"[WX849] 从文件缓存中获取群名: {group_name}")
                        
                        # 缓存群名
                        if not hasattr(self, "group_name_cache"):
                            self.group_name_cache = {}
                        self.group_name_cache[cache_key] = group_name
                        
                        # 检查是否需要更新群成员详情
                        need_update_members = not ("members" in chatrooms_info[group_id] and 
                                                len(chatrooms_info[group_id]["members"]) > 0)
                        
                        if need_update_members:
                            logger.debug(f"[WX849] 群 {group_id} 名称已缓存，但需要更新成员信息")
                            threading.Thread(target=lambda: asyncio.run(self._get_group_member_details(group_id))).start()
                        else:
                            logger.debug(f"[WX849] 群 {group_id} 信息已完整且未过期，无需更新")
                        
                        return group_name
                except Exception as e:
                    logger.error(f"[WX849] 从文件获取群名出错: {e}")
            
            logger.debug(f"[WX849] 群 {group_id} 信息不存在或已过期，需要从API获取")
            
            # 调用API获取群信息 - 使用群聊API
            params = {
                "QID": group_id,  # 群ID参数，正确的参数名是QID
                "Wxid": self.wxid  # 自己的wxid参数
            }
            
            try:
                # 获取API配置
                api_host = conf().get("wx849_api_host", "127.0.0.1")
                api_port = conf().get("wx849_api_port", 9000)
                protocol_version = conf().get("wx849_protocol_version", "849")
                
                # 确定API路径前缀
                if protocol_version == "855" or protocol_version == "ipad":
                    api_path_prefix = "/api"
                else:
                    api_path_prefix = "/VXAPI"
                
                # 构建完整的API URL用于日志
                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Group/GetChatRoomInfo"
                logger.debug(f"[WX849] 正在请求群信息API: {api_url}")
                logger.debug(f"[WX849] 请求参数: {json.dumps(params, ensure_ascii=False)}")  # 记录请求参数
                
                # 尝试使用群聊专用API
                group_info = await self._call_api("/Group/GetChatRoomInfo", params)
                
                # 保存群聊详情到统一的JSON文件
                try:
                    # 读取现有的群聊信息（如果存在）
                    chatrooms_info = {}
                    if os.path.exists(chatrooms_file):
                        try:
                            with open(chatrooms_file, 'r', encoding='utf-8') as f:
                                chatrooms_info = json.load(f)
                            logger.debug(f"[WX849] 已加载 {len(chatrooms_info)} 个现有群聊信息")
                        except Exception as e:
                            logger.error(f"[WX849] 加载现有群聊信息失败: {str(e)}")
                    
                    # 提取必要的群聊信息
                    if group_info and isinstance(group_info, dict):
                        # 递归函数用于查找特定key的值
                        def find_value(obj, key):
                            # 如果是字典
                            if isinstance(obj, dict):
                                # 直接检查当前字典
                                if key in obj:
                                    return obj[key]
                                # 检查带有"string"嵌套的字典
                                if key in obj and isinstance(obj[key], dict) and "string" in obj[key]:
                                    return obj[key]["string"]
                                # 递归检查字典的所有值
                                for k, v in obj.items():
                                    result = find_value(v, key)
                                    if result is not None:
                                        return result
                            # 如果是列表
                            elif isinstance(obj, list):
                                # 递归检查列表的所有项
                                for item in obj:
                                    result = find_value(item, key)
                                    if result is not None:
                                        return result
                            return None
                        
                        # 尝试提取群名称及其他信息
                        group_name = None
                        
                        # 首先尝试从NickName中获取
                        nickname_obj = find_value(group_info, "NickName")
                        if isinstance(nickname_obj, dict) and "string" in nickname_obj:
                            group_name = nickname_obj["string"]
                        elif isinstance(nickname_obj, str):
                            group_name = nickname_obj
                        
                        # 如果没找到，尝试其他可能的字段
                        if not group_name:
                            for name_key in ["ChatRoomName", "nickname", "name", "DisplayName"]:
                                name_value = find_value(group_info, name_key)
                                if name_value:
                                    if isinstance(name_value, dict) and "string" in name_value:
                                        group_name = name_value["string"]
                                    elif isinstance(name_value, str):
                                        group_name = name_value
                                    if group_name:
                                        break
                        
                        # 提取群主ID
                        owner_id = None
                        for owner_key in ["ChatRoomOwner", "chatroomowner", "Owner"]:
                            owner_value = find_value(group_info, owner_key)
                            if owner_value:
                                if isinstance(owner_value, dict) and "string" in owner_value:
                                    owner_id = owner_value["string"]
                                elif isinstance(owner_value, str):
                                    owner_id = owner_value
                                if owner_id:
                                    break
                        
                        # 检查群聊信息是否已存在
                        if group_id in chatrooms_info:
                            # 更新已有群聊信息
                            if group_name:
                                chatrooms_info[group_id]["nickName"] = group_name
                            if owner_id:
                                chatrooms_info[group_id]["chatRoomOwner"] = owner_id
                            chatrooms_info[group_id]["last_update"] = int(time.time())
                        else:
                            # 创建新群聊信息
                            chatrooms_info[group_id] = {
                                "chatroomId": group_id,
                                "nickName": group_name or group_id,
                                "chatRoomOwner": owner_id or "",
                                "members": [],
                                "last_update": int(time.time())
                            }
                        
                        # 保存到文件
                        with open(chatrooms_file, 'w', encoding='utf-8') as f:
                            json.dump(chatrooms_info, f, ensure_ascii=False, indent=2)
                        
                        logger.info(f"[WX849] 已更新群聊 {group_id} 基础信息")
                        
                        # 缓存群名
                        if group_name:
                            if not hasattr(self, "group_name_cache"):
                                self.group_name_cache = {}
                            self.group_name_cache[cache_key] = group_name
                            
                            # 异步获取群成员详情（不阻塞当前方法）
                            threading.Thread(target=lambda: asyncio.run(self._get_group_member_details(group_id))).start()
                            
                            return group_name
                    
                except Exception as save_err:
                    logger.error(f"[WX849] 保存群聊信息到文件失败: {save_err}")
                    import traceback
                    logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
                
                # 如果上面的处理没有返回群名称，再次尝试从原始数据中提取
                if group_info and isinstance(group_info, dict):
                    # 尝试从API返回中获取群名称
                    group_name = None
                    
                    # 尝试多种可能的字段名
                    possible_fields = ["NickName", "nickname", "ChatRoomName", "chatroomname", "DisplayName", "displayname"]
                    for field in possible_fields:
                        if field in group_info and group_info[field]:
                            group_name = group_info[field]
                            if isinstance(group_name, dict) and "string" in group_name:
                                group_name = group_name["string"]
                            break
                    
                    if group_name:
                        logger.debug(f"[WX849] 获取到群名称: {group_name}")
                        
                        # 缓存群名
                        if not hasattr(self, "group_name_cache"):
                            self.group_name_cache = {}
                        self.group_name_cache[cache_key] = group_name
                        
                        # 异步获取群成员详情
                        threading.Thread(target=lambda: asyncio.run(self._get_group_member_details(group_id))).start()
                        
                        return group_name
                    else:
                        logger.warning(f"[WX849] API返回成功但未找到群名称字段: {json.dumps(group_info, ensure_ascii=False)}")
                else:
                    logger.warning(f"[WX849] API返回无效数据: {group_info}")
            except Exception as e:
                # 详细记录API请求失败的错误信息
                logger.error(f"[WX849] 使用群聊API获取群名称失败: {e}")
                logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
                logger.error(f"[WX849] 请求参数: {json.dumps(params, ensure_ascii=False)}")
            
            # 如果无法获取群名，使用群ID作为名称
            logger.debug(f"[WX849] 无法获取群名称，使用群ID代替: {group_id}")
            # 缓存结果
            if not hasattr(self, "group_name_cache"):
                self.group_name_cache = {}
            self.group_name_cache[cache_key] = group_id
            
            # 尽管获取群名失败，仍然尝试获取群成员详情
            threading.Thread(target=lambda: asyncio.run(self._get_group_member_details(group_id))).start()
            
            return group_id
        except Exception as e:
            logger.error(f"[WX849] 获取群名称失败: {e}")
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return group_id

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """重写父类方法，构建消息上下文"""
        try:
            # 直接创建Context对象，确保结构正确
            context = Context()
            context.type = ctype
            context.content = content
            
            # 获取消息对象
            msg = kwargs.get('msg')
            
            # 检查是否是群聊消息
            isgroup = kwargs.get('isgroup', False)
            if isgroup and msg and hasattr(msg, 'from_user_id'):
                # 设置群组相关信息
                context["isgroup"] = True
                context["from_user_nickname"] = msg.sender_wxid  # 发送者昵称
                context["from_user_id"] = msg.sender_wxid  # 发送者ID
                context["to_user_id"] = msg.to_user_id  # 接收者ID
                context["other_user_id"] = msg.other_user_id or msg.from_user_id  # 群ID
                context["group_name"] = msg.from_user_id  # 临时使用群ID作为群名
                context["group_id"] = msg.from_user_id  # 群ID
                context["msg"] = msg  # 消息对象
                
                # 设置session_id为群ID
                context["session_id"] = msg.other_user_id or msg.from_user_id
                
                # 启动异步任务获取群名称并更新
                loop = asyncio.get_event_loop()
                try:
                    # 尝试创建异步任务获取群名
                    async def update_group_name():
                        try:
                            group_name = await self._get_group_name(msg.from_user_id)
                            if group_name:
                                context['group_name'] = group_name
                                logger.debug(f"[WX849] 更新群名称: {group_name}")
                        except Exception as e:
                            logger.error(f"[WX849] 更新群名称失败: {e}")
                    
                    # 使用已有事件循环运行更新任务
                    def run_async_task():
                        try:
                            asyncio.run(update_group_name())
                        except Exception as e:
                            logger.error(f"[WX849] 异步获取群名称任务失败: {e}")
                    
                    # 启动线程执行异步任务
                    threading.Thread(target=run_async_task).start()
                except Exception as e:
                    logger.error(f"[WX849] 创建获取群名称任务失败: {e}")
            else:
                # 私聊消息
                context["isgroup"] = False
                context["from_user_nickname"] = msg.sender_wxid if msg and hasattr(msg, 'sender_wxid') else ""
                context["from_user_id"] = msg.sender_wxid if msg and hasattr(msg, 'sender_wxid') else ""
                context["to_user_id"] = msg.to_user_id if msg and hasattr(msg, 'to_user_id') else ""
                context["other_user_id"] = None
                context["msg"] = msg
                
                # 设置session_id为发送者ID
                context["session_id"] = msg.sender_wxid if msg and hasattr(msg, 'sender_wxid') else ""

            # 添加接收者信息
            context["receiver"] = msg.from_user_id if isgroup else msg.sender_wxid
            
            # 记录原始消息类型
            context["origin_ctype"] = ctype
            
            # 添加调试日志
            logger.debug(f"[WX849] 生成Context对象: type={context.type}, content={context.content}, isgroup={context['isgroup']}, session_id={context.get('session_id', 'None')}")
            
            return context
        except Exception as e:
            logger.error(f"[WX849] 构建上下文失败: {e}")
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None

    async def _get_chatroom_member_nickname(self, group_id, member_wxid):
        """获取群成员的昵称"""
        if not group_id or not member_wxid:
            return member_wxid
            
        try:
            # 优先从缓存获取群成员信息
            tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp")
            chatrooms_file = os.path.join(tmp_dir, 'wx849_rooms.json')
            
            if os.path.exists(chatrooms_file):
                with open(chatrooms_file, 'r', encoding='utf-8') as f:
                    chatrooms_info = json.load(f)
                
                if group_id in chatrooms_info and "members" in chatrooms_info[group_id]:
                    for member in chatrooms_info[group_id]["members"]:
                        if member.get("UserName") == member_wxid:
                            # 优先使用群内显示名称(群昵称)
                            if member.get("DisplayName"):
                                logger.debug(f"[WX849] 获取到成员 {member_wxid} 的群昵称: {member.get('DisplayName')}")
                                return member.get("DisplayName")
                            # 其次使用成员昵称
                            elif member.get("NickName"):
                                logger.debug(f"[WX849] 获取到成员 {member_wxid} 的昵称: {member.get('NickName')}")
                                return member.get("NickName")
            
            # 如果缓存中没有，尝试更新群成员信息
            await self._get_group_member_details(group_id)
            
            # 再次尝试从更新后的缓存中获取
            if os.path.exists(chatrooms_file):
                with open(chatrooms_file, 'r', encoding='utf-8') as f:
                    chatrooms_info = json.load(f)
                
                if group_id in chatrooms_info and "members" in chatrooms_info[group_id]:
                    for member in chatrooms_info[group_id]["members"]:
                        if member.get("UserName") == member_wxid:
                            # 优先使用群内显示名称
                            if member.get("DisplayName"):
                                logger.debug(f"[WX849] 更新后获取到成员 {member_wxid} 的群昵称: {member.get('DisplayName')}")
                                return member.get("DisplayName")
                            # 其次使用成员昵称
                            elif member.get("NickName"):
                                logger.debug(f"[WX849] 更新后获取到成员 {member_wxid} 的昵称: {member.get('NickName')}")
                                return member.get("NickName")
        except Exception as e:
            logger.error(f"[WX849] 获取群成员昵称出错: {e}")
        
        # 默认返回wxid
        return member_wxid

    async def _get_current_login_wxid(self):
        """获取当前API服务器登录的微信账号"""
        try:
            # 尝试通过profile接口获取当前登录账号
            response = await self._call_api("/User/Profile", {"Wxid": ""})
            
            if response and isinstance(response, dict) and response.get("Success", False):
                data = response.get("Data", {})
                userinfo = data.get("userInfo", {})
                # 尝试获取userName，这通常是wxid
                if "userName" in userinfo:
                    return userinfo["userName"]
                # 尝试获取UserName，有些版本可能是大写
                elif "UserName" in userinfo:
                    return userinfo["UserName"]
                # 尝试获取string结构中的wxid
                elif isinstance(userinfo, dict):
                    for key in ["userName", "UserName"]:
                        if key in userinfo and isinstance(userinfo[key], dict) and "string" in userinfo[key]:
                            return userinfo[key]["string"]
            
            # 如果以上方法都失败，尝试通过其他接口
            response = await self._call_api("/User/GetSelfInfo", {})
            if response and isinstance(response, dict) and response.get("Success", False):
                data = response.get("Data", {})
                return data.get("Wxid", "")
                
            return ""
        except Exception as e:
            logger.error(f"[WX849] 获取当前登录账号失败: {e}")
            return ""
            
    async def _check_api_login_consistency(self, saved_wxid):
        """检查API服务器登录的账号是否与保存的一致"""
        try:
            # 尝试获取当前登录的用户信息
            profile = await self.bot.get_profile()
            
            if not profile or not isinstance(profile, dict):
                logger.warning("[WX849] 获取用户资料失败，无法确认登录一致性")
                return False
            
            # 提取当前登录用户的wxid
            current_wxid = None
            userinfo = profile.get("userInfo", {})
            
            if isinstance(userinfo, dict):
                if "wxid" in userinfo:
                    current_wxid = userinfo["wxid"]
                elif "userName" in userinfo:
                    current_wxid = userinfo["userName"]
                elif "UserName" in userinfo:
                    current_wxid = userinfo["UserName"]
            
            # 如果没有获取到当前wxid，返回False
            if not current_wxid:
                logger.warning("[WX849] 无法从用户资料中获取wxid，无法确认登录一致性")
                return False
            
            # 比较当前wxid与保存的wxid是否一致
            is_consistent = (current_wxid == saved_wxid)
            
            if is_consistent:
                logger.info(f"[WX849] API服务器登录用户与本地保存一致: {saved_wxid}")
            else:
                logger.warning(f"[WX849] API服务器登录用户 ({current_wxid}) 与本地保存 ({saved_wxid}) 不一致")
            
            return is_consistent
        except Exception as e:
            logger.error(f"[WX849] 检查登录一致性失败: {e}")
            return False

    async def _refresh_token(self, wxid, device_id=None):
        """处理token过期问题"""
        try:
            # 尝试使用RefreshToken接口
            params = {
                "wxid": wxid  # 参数名改为小写
            }
            if device_id:
                params["device_id"] = device_id
                
            response = await self._call_api("/Login/RefreshToken", params)
            
            if response and isinstance(response, dict) and response.get("Success", False):
                logger.info("[WX849] 成功刷新token")
                return True
                
            # 如果刷新失败，尝试二次登录
            logger.info("[WX849] 刷新token失败，尝试二次登录")
            login_result = await self._twice_login(wxid, device_id)
            
            return login_result
        except Exception as e:
            logger.error(f"[WX849] 刷新token失败: {e}")
            return False
            
    async def _process_api_response(self, response, wxid=None, device_id=None):
        """处理API响应，检查是否需要刷新token"""
        if not response:
            return response
            
        if isinstance(response, dict):
            # 检查是否返回token过期错误
            error_code = response.get("code", 0)
            error_msg = response.get("message", "")
            
            token_expired_codes = [40014, 40016, 41001, 42001, 42002, 42003]
            token_expired_messages = ["token expired", "invalid token", "token invalid", "access_token expired"]
            
            is_token_expired = (error_code in token_expired_codes) or any(msg in error_msg.lower() for msg in token_expired_messages)
            
            if is_token_expired and wxid:
                logger.warning(f"[WX849] 检测到token过期问题: {error_code} - {error_msg}")
                success = await self._refresh_token(wxid, device_id)
                if success:
                    logger.info("[WX849] 刷新token成功，重试请求")
                    # 这里需要返回一个特殊值，告知调用方需要重试请求
                    return {"__retry_needed__": True}
                else:
                    logger.error("[WX849] 刷新token失败")
        
        return response

    async def _send_app_xml(self, to_user_id, xml_content, app_type: int):
        """发送App XML消息的异步方法 (使用 _call_api 和 /Msg/SendApp 端点)"""
        try:
            if not to_user_id:
                logger.error("[WX849] Send App XML failed: receiver ID is empty")
                return None
            if not xml_content or not isinstance(xml_content, str):
                logger.error("[WX849] Send App XML failed: XML content is invalid or not a string")
                return None
            if not xml_content.strip():
                logger.error("[WX849] Send App XML failed: XML content is empty string")
                return None

            params = {
                "ToWxid": to_user_id,
                "Xml": xml_content, 
                "Type": app_type,   
                "wxid": self.wxid
            }
            
            logger.debug(f"[WX849] Calling _call_api for App XML. Endpoint: /Msg/SendApp, Params: Wxid={params['wxid']}, ToWxid={params['ToWxid']}, Type={params['Type']}, Xml snippet={xml_content[:100]}...")
            
            # Using the endpoint found in WechatAPIClient source, _call_api will prepend /api or /VXAPI
            result = await self._call_api("/Msg/SendApp", params)

            if result and isinstance(result, dict):
                success = result.get("Success", False)
                if not success:
                    error_msg = result.get("Message", "Unknown error after _call_api")
                    logger.error(f"[WX849] _call_api for Send App XML indicated failure: {error_msg}. API Result: {result}")
            else:
                logger.error(f"[WX849] _call_api for Send App XML returned invalid result: {result}")
            return result # Return the result from _call_api
                        
        except Exception as e:
            logger.error(f"[WX849] Send App XML failed (General Exception in _send_app_xml): {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Ensure a consistent error response structure if needed by the caller
            return {"Success": False, "Message": f"Exception in _send_app_xml: {e}"}

    async def _send_voice(self, to_user_id, voice_file_path_segment):
        """发送语音消息的异步方法 (单个MP3片段路径), 内部处理SILK转换."""
        if not PYSLIK_AVAILABLE:
            logger.error("[WX849] Send voice failed: pysilk library is not available.")
            return {"Success": False, "Message": "pysilk library not available"}

        try:
            if not to_user_id:
                logger.error("[WX849] Send voice failed: receiver ID is empty")
                return {"Success": False, "Message": "Receiver ID empty"}
            if not os.path.exists(voice_file_path_segment):
                logger.error(f"[WX849] Send voice failed: voice segment file not found at {voice_file_path_segment}")
                return {"Success": False, "Message": f"Voice segment not found: {voice_file_path_segment}"}

            # Load MP3 segment with pydub
            try:
                # Ensure BytesIO is used if pydub's from_file expects a file-like object for all inputs
                # or if voice_file_path_segment might not always be a simple path string.
                # However, for a path string, direct usage is fine.
                audio = AudioSegment.from_file(voice_file_path_segment, format="mp3")
            except Exception as e_pydub_load:
                logger.error(f"[WX849] Failed to load voice segment {voice_file_path_segment} with pydub: {e_pydub_load}")
                logger.error(traceback.format_exc()) # Log full traceback for pydub errors
                return {"Success": False, "Message": f"Pydub load failed: {e_pydub_load}"}

            # Process audio: set channels, frame rate
            audio = audio.set_channels(1)
            supported_rates = [8000, 12000, 16000, 24000] # SILK supported rates
            closest_rate = min(supported_rates, key=lambda x: abs(x - audio.frame_rate))
            audio = audio.set_frame_rate(closest_rate)
            duration_ms = len(audio)

            if duration_ms == 0:
                logger.warning(f"[WX849] Voice segment {voice_file_path_segment} has zero duration after pydub processing. Skipping send.")
                return {"Success": False, "Message": "Zero duration audio"}

            # Encode to SILK using pysilk
            try:
                if hasattr(pysilk, 'async_encode') and asyncio.iscoroutinefunction(pysilk.async_encode):
                    silk_data = await pysilk.async_encode(audio.raw_data, sample_rate=audio.frame_rate)
                elif hasattr(pysilk, 'encode'): 
                    silk_data = pysilk.encode(audio.raw_data, sample_rate=audio.frame_rate)
                else:
                    logger.error("[WX849] pysilk does not have a usable 'encode' or 'async_encode' method.")
                    return {"Success": False, "Message": "pysilk encode method not found"}
            except Exception as e_silk_encode:
                logger.error(f"[WX849] SILK encoding failed for {voice_file_path_segment}: {e_silk_encode}")
                logger.error(traceback.format_exc()) # Log full traceback for silk errors
                return {"Success": False, "Message": f"SILK encoding failed: {e_silk_encode}"}
            
            voice_base64 = base64.b64encode(silk_data).decode('utf-8')

            params = {
                "ToWxid": to_user_id,
                "Wxid": self.wxid,
                "Base64": voice_base64,
                "Type": 4, 
                "VoiceTime": int(duration_ms)
            }
            
            logger.info(f"[WX849] Preparing to send SILK voice: ToWxid={to_user_id}, File={voice_file_path_segment}, VoiceTime={duration_ms}ms, Type=4")
            
            result = await self._call_api("/Msg/SendVoice", params)
            
            if result and result.get("Success"):
                logger.info(f"[WX849] Send SILK voice success: ToWxid={to_user_id}, File={voice_file_path_segment}, Result: {result}")
            else:
                logger.error(f"[WX849] Send SILK voice failed: ToWxid={to_user_id}, File={voice_file_path_segment}, Result: {result}")
            return result

        except Exception as e:
            logger.error(f"[WX849] Exception in _send_voice (SILK processing) for {voice_file_path_segment} to {to_user_id}: {e}")
            logger.error(traceback.format_exc())
            return {"Success": False, "Message": f"General exception in _send_voice: {e}"}

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """重写父类方法，构建消息上下文"""
        try:
            # 直接创建Context对象，确保结构正确
            context = Context()
            context.type = ctype
            context.content = content
            
            # 获取消息对象
            msg = kwargs.get('msg')
            
            # 检查是否是群聊消息
            isgroup = kwargs.get('isgroup', False)
            if isgroup and msg and hasattr(msg, 'from_user_id'):
                # 设置群组相关信息
                context["isgroup"] = True
                context["from_user_nickname"] = msg.sender_wxid  # 发送者昵称
                context["from_user_id"] = msg.sender_wxid  # 发送者ID
                context["to_user_id"] = msg.to_user_id  # 接收者ID
                context["other_user_id"] = msg.other_user_id or msg.from_user_id  # 群ID
                context["group_name"] = msg.from_user_id  # 临时使用群ID作为群名
                context["group_id"] = msg.from_user_id  # 群ID
                context["msg"] = msg  # 消息对象
                
                # 设置session_id为群ID
                context["session_id"] = msg.other_user_id or msg.from_user_id
                
                # 启动异步任务获取群名称并更新
                loop = asyncio.get_event_loop()
                try:
                    # 尝试创建异步任务获取群名
                    async def update_group_name():
                        try:
                            group_name = await self._get_group_name(msg.from_user_id)
                            if group_name:
                                context['group_name'] = group_name
                                logger.debug(f"[WX849] 更新群名称: {group_name}")
                        except Exception as e:
                            logger.error(f"[WX849] 更新群名称失败: {e}")
                    
                    # 使用已有事件循环运行更新任务
                    def run_async_task():
                        try:
                            asyncio.run(update_group_name())
                        except Exception as e:
                            logger.error(f"[WX849] 异步获取群名称任务失败: {e}")
                    
                    # 启动线程执行异步任务
                    threading.Thread(target=run_async_task).start()
                except Exception as e:
                    logger.error(f"[WX849] 创建获取群名称任务失败: {e}")
            else:
                # 私聊消息
                context["isgroup"] = False
                context["from_user_nickname"] = msg.sender_wxid if msg and hasattr(msg, 'sender_wxid') else ""
                context["from_user_id"] = msg.sender_wxid if msg and hasattr(msg, 'sender_wxid') else ""
                context["to_user_id"] = msg.to_user_id if msg and hasattr(msg, 'to_user_id') else ""
                context["other_user_id"] = None
                context["msg"] = msg
                
                # 设置session_id为发送者ID
                context["session_id"] = msg.sender_wxid if msg and hasattr(msg, 'sender_wxid') else ""

            # 添加接收者信息
            context["receiver"] = msg.from_user_id if isgroup else msg.sender_wxid
            
            # 记录原始消息类型
            context["origin_ctype"] = ctype
            
            # 添加调试日志
            logger.debug(f"[WX849] 生成Context对象: type={context.type}, content={context.content}, isgroup={context['isgroup']}, session_id={context.get('session_id', 'None')}")
            
            return context
        except Exception as e:
            logger.error(f"[WX849] 构建上下文失败: {e}")
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None
