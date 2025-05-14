import asyncio
import os
import re
import json
import time
import threading
import io
import sys
import traceback 
import xml.etree.ElementTree as ET  
import cv2
import aiohttp
import uuid 
from typing import Union, BinaryIO, Optional, Tuple, List, Dict
import urllib.parse  
import requests
from bridge.context import Context, ContextType  
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.chat_message import ChatMessage
from channel.wx849.wx849_message import WX849Message  # 改为从wx849_message导入WX849Message
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.time_check import time_checker
from common.utils import remove_markdown_symbol, split_string_by_utf8_length
from config import conf, get_appdata_dir
from voice.audio_convert import split_audio # Added for voice splitting
from common.tmp_dir import TmpDir # Added for temporary file management
from plugins import PluginManager, EventContext, Event
# 新增HTTP服务器相关导入
from aiohttp import web
from pathlib import Path
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
        self.image_cache_dir = os.path.join(os.getcwd(), "tmp", "wx849_img_cache")
        try:
            if not os.path.exists(self.image_cache_dir):
                os.makedirs(self.image_cache_dir, exist_ok=True)
                logger.info(f"[{self.name}] Created image cache directory: {self.image_cache_dir}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to create image cache directory {self.image_cache_dir}: {e}")

    def _cleanup_cached_images(self):
        """Cleans up expired image files from the cache directory."""
        if not hasattr(self, 'image_cache_dir') or not self.image_cache_dir:
            logger.warning(f"[{self.name}] Image cache directory not configured. Skipping cleanup.")
            return
        
        logger.info(f"[{self.name}] Starting image cache cleanup in {self.image_cache_dir}...")
        try:
            current_time = time.time()
            max_age_seconds = 7 * 24 * 60 * 60  # Cache images for 7 days

            # Iterate over common image extensions used for caching
            # Ensure this matches extensions used during caching (see phase A3)
            for ext_pattern in ['*.jpg', '*.jpeg', '*.png', '*.gif']: 
                pattern = os.path.join(self.image_cache_dir, ext_pattern)
                cleaned_count = 0
                total_size_cleaned = 0

                for fpath in glob.glob(pattern):
                    try:
                        if os.path.isfile(fpath): # Ensure it's a file
                            mtime = os.path.getmtime(fpath)
                            if current_time - mtime > max_age_seconds:
                                file_size = os.path.getsize(fpath)
                                os.remove(fpath)
                                cleaned_count += 1
                                total_size_cleaned += file_size
                                logger.debug(f"[{self.name}] Cleaned up expired cached image: {fpath} (Age: {(current_time - mtime)/3600/24:.1f} days)")
                    except Exception as e:
                        logger.warning(f"[{self.name}] Failed to process/delete cached image {fpath}: {e}")
                
                if cleaned_count > 0:
                    logger.info(f"[{self.name}] Cleaned up {cleaned_count} '{ext_pattern}' images, freed {total_size_cleaned/1024/1024:.2f} MB.")
            logger.info(f"[{self.name}] Image cache cleanup finished.")
        except Exception as e:
            logger.error(f"[{self.name}] Image cache cleanup task encountered an error: {e}")

    def _start_image_cache_cleanup_task(self):
        """Starts the periodic image cache cleanup task."""
        if not hasattr(self, 'image_cache_dir'): # Don't start if cache isn't configured
            return

        def _cleanup_loop():
            logger.info(f"[{self.name}] Image cache cleanup thread started.")
            # Initial delay before first cleanup, e.g., 5 minutes after startup
            time.sleep(5 * 60) 
            while True:
                try:
                    self._cleanup_cached_images()
                    # Sleep for a longer interval, e.g., 6 hours or 24 hours
                    cleanup_interval_hours = 24 
                    logger.debug(f"[{self.name}] Image cache cleanup task sleeping for {cleanup_interval_hours} hours.")
                    time.sleep(cleanup_interval_hours * 60 * 60)
                except Exception as e:
                    logger.error(f"[{self.name}] Image cache cleanup loop error: {e}. Retrying in 1 hour.")
                    time.sleep(60 * 60) # Wait an hour before retrying the loop on major error

        cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
        cleanup_thread.name = "WX849ImageCacheCleanupThread"
        cleanup_thread.start()
        logger.info(f"[{self.name}] Image cache cleanup task scheduled.")

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

                            # ADDED: Call the new filter method
                            if self._should_filter_this_message(cmsg): # 调用新的过滤方法
                                logger.debug(f"[WX849] Message from {getattr(cmsg, 'sender_wxid', 'UnknownSender')} was filtered out by _should_filter_this_message.")
                                continue # 如果消息被过滤，则跳过后续处理，处理下一条消息                            

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
        self.loop = loop
        self._start_image_cache_cleanup_task()         
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

    # MODIFIED: New filter method with corrected sender ID logic for gh_ check
    def _should_filter_this_message(self, wx_msg: 'WX849Message') -> bool:
        # 过滤非用户消息
        # import time
        # from bridge.context import ContextType
        # from config import conf
        # from .wx849_message import WX849Message # Assuming WX849Message is in wx849_message.py
        # logger should be defined (e.g., import logging; logger = logging.getLogger(__name__))

        if not wx_msg:
            logger.debug("[WX849] Filter: Received an empty message object, ignoring.")
            return True

        # Get primary identifiers from wx_msg
        # actual_from_user_id is the direct sender (e.g., a user in private chat, a group ID, or a gh_ ID)
        actual_from_user_id = getattr(wx_msg, 'from_user_id', '')
        # actual_sender_wxid is specifically for the user who sent the message within a group
        actual_sender_wxid = getattr(wx_msg, 'sender_wxid', '') 

        # Determine the most relevant sender ID for general filtering/logging after the gh_ check.
        # If actual_sender_wxid is populated (usually in groups for a specific user), it's preferred.
        # Otherwise (e.g., private chat, or if sender_wxid wasn't parsed from group msg), use actual_from_user_id.
        effective_sender_id = actual_sender_wxid if actual_sender_wxid else actual_from_user_id

        _content_value = getattr(wx_msg, 'content', '') 
        _message_content_preview = f"(content type: {type(_content_value)}, first 50 chars: {str(_content_value)[:50]})"
        _message_type = getattr(wx_msg, 'type', None) # wx_msg.type should be ContextType
        _message_create_time = getattr(wx_msg, 'create_time', None)

        # 1. Ignore non-user messages (e.g., official accounts starting with 'gh_')
        #    This check specifically uses actual_from_user_id.
        if isinstance(actual_from_user_id, str) and actual_from_user_id.startswith("gh_"):
            logger.debug(f"[WX849] Filter: Ignored official account message from {actual_from_user_id}: {_message_content_preview}")
            return True

        # 2. Ignore voice messages if speech recognition is off
        if _message_type == ContextType.VOICE:
            if conf().get("speech_recognition") != True:
                logger.debug(f"[WX849] Filter: Ignored voice message (speech recognition off): from {effective_sender_id}")
                return True

        # 3. Ignore messages from self (self.user_id should be the bot's own WXID)
        if self.user_id and effective_sender_id == self.user_id: 
            logger.debug(f"[WX849] Filter: Ignored message from myself ({self.user_id}): {_message_content_preview}")
            return True

        # 4. Ignore expired messages (e.g., older than 5 minutes)
        if _message_create_time:
            try:
                msg_ts = float(_message_create_time)
                current_ts = time.time()
                if msg_ts < (current_ts - 300):  # 300 seconds = 5 minutes
                    logger.debug(f"[WX849] Filter: Ignored expired message (timestamp: {msg_ts}) from {effective_sender_id}: {_message_content_preview}")
                    return True
            except (ValueError, TypeError):
                logger.warning(f"[WX849] Filter: Could not parse create_time '{_message_create_time}' for sender {effective_sender_id}.")
            except Exception as e: 
                logger.warning(f"[WX849] Filter: Error checking expired message for sender {effective_sender_id}: {e}")
        
        # 5. Ignore status sync messages
        if hasattr(ContextType, 'STATUS_SYNC') and _message_type == ContextType.STATUS_SYNC:
            logger.debug(f"[WX849] Filter: Ignored status sync message from {effective_sender_id}: {_message_content_preview}")
            return True
        
        # Duplicate message check
        # Use effective_sender_id for the duplicate key to ensure uniqueness.
        if wx_msg and hasattr(wx_msg, 'msg_id') and wx_msg.msg_id:
            # Ensure received_msgs is initialized in WX849Channel.__init__
            # e.g., self.received_msgs = ExpiredDict(conf().get("expires_in_seconds", 3600))
            if not hasattr(self, 'received_msgs'):
                 logger.error("[WX849] Filter: self.received_msgs is not initialized. Cannot check for duplicates.")
            else:
                wx_msg_key = f"{wx_msg.msg_id}_{effective_sender_id}_{wx_msg.create_time}"
                if wx_msg_key in self.received_msgs: 
                    logger.debug(f"[WX849] Filter: Ignored duplicate message: {wx_msg_key}")
                    return True
                self.received_msgs[wx_msg_key] = wx_msg
        else:
            logger.debug("[WX849] Filter: Message lacks unique msg_id for duplicate check, proceeding with caution.")
        
        return False # Message passed all filters


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
                
                # MODIFIED: Enhanced prefix checking for normal and quote messages
                text_to_check_for_prefix = cmsg.content
                is_quote_with_extracted_question = False
                guide_prefix = ""
                original_user_question_in_quote = ""
                guide_suffix = "" # This will capture the quote marks and newlines after the user question

                if hasattr(cmsg, 'is_processed_text_quote') and cmsg.is_processed_text_quote:
                    # 确保 re 模块在这里是可用的
                    import re # <--- 在这里显式导入一次
                    match = re.match(r'(用户针对以下(?:消息|聊天记录)提问：")(.*?)("\n\n)', cmsg.content, re.DOTALL)
                    if match:
                        guide_prefix = match.group(1)  # "用户针对以下消息提问：""
                        original_user_question_in_quote = match.group(2) # "xy他说什么"
                        guide_suffix = match.group(3)    # "”\n\n"
                        text_to_check_for_prefix = original_user_question_in_quote
                        is_quote_with_extracted_question = True
                        logger.debug(f"[WX849] Quote message: Extracted text for prefix check: '{text_to_check_for_prefix}'")
                    else:
                        logger.debug(f"[WX849] Quote message format did not match extraction pattern: {cmsg.content[:100]}...")
                
                # Loop through configured prefixes
                for prefix in group_chat_prefix:
                    if prefix and text_to_check_for_prefix.startswith(prefix):
                        logger.debug(f"[WX849] Group chat matched prefix: '{prefix}' (on text: '{text_to_check_for_prefix[:50]}...')")
                        cleaned_question_content = text_to_check_for_prefix[len(prefix):].strip()
                        
                        if is_quote_with_extracted_question:
                            # Reconstruct cmsg.content with the cleaned question part, preserving the rest of the quote structure
                            # The rest of the message starts after the original full guide + question + suffix part
                            full_original_question_segment = guide_prefix + original_user_question_in_quote + guide_suffix
                            if cmsg.content.startswith(full_original_question_segment):
                                rest_of_message_after_quote_question = cmsg.content[len(full_original_question_segment):]
                                cmsg.content = guide_prefix + cleaned_question_content + guide_suffix + rest_of_message_after_quote_question
                                logger.debug(f"[WX849] Quote message, prefix removed. New content: {cmsg.content[:150]}...")
                            else:
                                # This fallback is less ideal as it might indicate an issue with segment identification
                                logger.warning(f"[WX849] Quote message content did not start as expected with extracted segments. Attempting direct replacement of user question part.")
                                # Attempt to replace only the original_user_question_in_quote part within the larger cmsg.content
                                # This is safer if the rest_of_message_after_quote_question logic is not robust enough for all cases
                                cmsg.content = cmsg.content.replace(original_user_question_in_quote, cleaned_question_content, 1)
                                logger.debug(f"[WX849] Quote message, prefix removed via replace. New content: {cmsg.content[:150]}...")
                        else:
                            # For non-quote messages, the behavior is as before
                            cmsg.content = cleaned_question_content
                            logger.debug(f"[WX849] Non-quote message, prefix removed. New content: {cmsg.content}")
                        
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

    async def _process_image_message(self, cmsg: WX849Message): # Added WX849Message type hint
        """处理图片消息"""
        import xml.etree.ElementTree as ET
        import os

        import time
        # import threading # Not used directly in this snippet
        import traceback # Added for logging
        from bridge.context import ContextType # Added for ContextType

        # 在这里不检查和标记图片消息，而是在图片下载完成后再标记
        # 这样可以确保图片消息被正确处理为IMAGE类型，而不是UNKNOWN类型

        cmsg.ctype = ContextType.IMAGE

        # 处理群聊/私聊消息发送者
        if cmsg.is_group or (hasattr(cmsg, 'from_user_id') and cmsg.from_user_id and cmsg.from_user_id.endswith("@chatroom")):
            cmsg.is_group = True # Ensure is_group is set
            # Ensure cmsg.content is a string before splitting
            if isinstance(cmsg.content, str):
                split_content = cmsg.content.split(":\n", 1)
                if len(split_content) > 1:
                    cmsg.sender_wxid = split_content[0]
                    cmsg.content = split_content[1] # This will be the XML part
                else:
                    # 处理没有换行的情况 (less likely for image XML, but good to have)
                    split_content_alt = cmsg.content.split(":", 1)
                    if len(split_content_alt) > 1:
                        # This case might be problematic if XML itself contains ':' early
                        # For images, content is usually just the XML
                        # Assuming if split by ':\n' fails, the content is the XML itself
                        if not hasattr(cmsg, 'sender_wxid') or not cmsg.sender_wxid:
                             cmsg.sender_wxid = cmsg.actual_user_id if hasattr(cmsg, 'actual_user_id') else ""
                        # cmsg.content remains as is if no ":\n"
                    else:
                        cmsg.sender_wxid = cmsg.actual_user_id if hasattr(cmsg, 'actual_user_id') else ""
            else: # cmsg.content is not a string (e.g. already bytes)
                 cmsg.sender_wxid = cmsg.actual_user_id if hasattr(cmsg, 'actual_user_id') else ""


            # 设置actual_user_id和actual_user_nickname (should be done by the calling _process_message)
            if not hasattr(cmsg, 'actual_user_id') or not cmsg.actual_user_id:
                cmsg.actual_user_id = cmsg.sender_wxid
            if not hasattr(cmsg, 'actual_user_nickname') or not cmsg.actual_user_nickname:
                cmsg.actual_user_nickname = cmsg.sender_wxid # Placeholder if no real nickname available
        else:
            # 私聊消息
            cmsg.sender_wxid = cmsg.from_user_id
            cmsg.is_group = False

            # 私聊消息也设置actual_user_id和actual_user_nickname
            cmsg.actual_user_id = cmsg.from_user_id
            cmsg.actual_user_nickname = cmsg.from_user_id # Placeholder

        # 解析图片信息
        try:
            xml_content_to_parse = ""
            if isinstance(cmsg.content, str) and (cmsg.content.startswith('<?xml') or cmsg.content.startswith("<msg>")):
                xml_content_to_parse = cmsg.content
            # Add handling if cmsg.content might be bytes that need decoding
            elif isinstance(cmsg.content, bytes):
                try:
                    xml_content_to_parse = cmsg.content.decode('utf-8')
                    if not (xml_content_to_parse.startswith('<?xml') or xml_content_to_parse.startswith("<msg>")):
                        xml_content_to_parse = "" # Not valid XML
                except UnicodeDecodeError:
                    logger.warning(f"[{self.name}] Msg {cmsg.msg_id}: Image content is bytes but failed to decode as UTF-8.")
                    xml_content_to_parse = ""

            if xml_content_to_parse:
                try:
                    root = ET.fromstring(xml_content_to_parse)
                    img_element = root.find('img')
                    if img_element is not None:
                        # MODIFICATION START: Store aeskey and other info on cmsg directly
                        cmsg.img_aeskey = img_element.get('aeskey')
                        cmsg.img_cdnthumbaeskey = img_element.get('cdnthumbaeskey') # Optional
                        cmsg.img_md5 = img_element.get('md5') # Optional
                        cmsg.img_length = img_element.get('length', '0')
                        cmsg.img_cdnmidimgurl = img_element.get('cdnmidimgurl', '')
                        # MODIFICATION END

                        # Use a combined dictionary for logging for clarity
                        cmsg.image_info = {
                            'aeskey': cmsg.img_aeskey,
                            'cdnmidimgurl': cmsg.img_cdnmidimgurl,
                            'length': cmsg.img_length,
                            'md5': cmsg.img_md5
                        }
                        logger.debug(f"[{self.name}] Msg {cmsg.msg_id}: Parsed image XML: aeskey={cmsg.img_aeskey}, length={cmsg.img_length}, md5={cmsg.img_md5}")
                        
                        if not cmsg.img_aeskey:
                             logger.warning(f"[{self.name}] Msg {cmsg.msg_id}: Image XML 'aeskey' is missing. Caching by aeskey will not be possible.")
                    else:
                        logger.warning(f"[{self.name}] Msg {cmsg.msg_id}: XML in content but no <img> tag found. Content (first 100): {xml_content_to_parse[:100]}")
                        # Initialize attributes on cmsg to prevent AttributeError later
                        cmsg.img_aeskey = None
                        cmsg.img_length = '0'
                        # Create a default image_info for compatibility if other parts expect it
                        cmsg.image_info = {'aeskey': '', 'cdnmidimgurl': '', 'length': '0', 'md5': ''}
                except ET.ParseError as xml_err:
                    logger.warning(f"[{self.name}] Msg {cmsg.msg_id}: Failed to parse image XML: {xml_err}. Content (first 100): {xml_content_to_parse[:100]}")
                    cmsg.img_aeskey = None
                    cmsg.img_length = '0'
                    cmsg.image_info = {'aeskey': '', 'cdnmidimgurl': '', 'length': '0', 'md5': ''}
            else:
                # Content is not XML (could be a path if already processed by another layer, or unexpected format)
                logger.warning(f"[{self.name}] Msg {cmsg.msg_id}: Image content is not XML. Content (first 100): {str(cmsg.content)[:100]}")
                cmsg.img_aeskey = None # Ensure it's defined
                cmsg.img_length = '0'
                cmsg.image_info = {'aeskey': '', 'cdnmidimgurl': '', 'length': '0', 'md5': ''} # Default

            # Download logic (largely from your snippet)
            # Check if image_path is already set and valid
            if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
                logger.info(f"[{self.name}] Msg {cmsg.msg_id}: Image already exists at path: {cmsg.image_path}")
            else:

                locks_tmp_dir = os.path.join(os.path.dirname(self.image_cache_dir) if hasattr(self, 'image_cache_dir') else os.path.join(os.getcwd(), "tmp"), "img_locks")

                try:
                    os.makedirs(locks_tmp_dir, exist_ok=True)
                except Exception as e_mkdir:
                     logger.error(f"[{self.name}] Failed to create lock directory {locks_tmp_dir}: {e_mkdir}")
                     # Potentially skip download if lock dir cannot be made, or try without lock

                lock_file = os.path.join(locks_tmp_dir, f"img_{cmsg.msg_id}.lock")

                if os.path.exists(lock_file):
                    # Check lock file age, could be stale
                    try:
                        lock_time = os.path.getmtime(lock_file)
                        if (time.time() - lock_time) < 300: # 5-minute timeout for stale lock
                            logger.info(f"[{self.name}] Image {cmsg.msg_id} is likely being downloaded by another thread (lock active). Skipping.")
                            return # Skip if lock is recent
                        else:
                            logger.warning(f"[{self.name}] Image {cmsg.msg_id} lock file is stale. Removing and attempting download.")
                            os.remove(lock_file)
                    except Exception as e_lock_check:
                        logger.warning(f"[{self.name}] Error checking stale lock for {cmsg.msg_id}: {e_lock_check}. Proceeding with caution.")
                
                download_attempted = False
                try:
                    # Create lock file
                    with open(lock_file, "w") as f:
                        f.write(str(time.time()))
                    
                    download_attempted = True
                    logger.info(f"[{self.name}] Msg {cmsg.msg_id}: Attempting to download image.")
                    # Asynchronously download the image
                    # _download_image should set cmsg.image_path upon success
                    await self._download_image(cmsg) 
                    
                except Exception as e:
                    logger.error(f"[{self.name}] Msg {cmsg.msg_id}: Failed to download image: {e}")
                    logger.error(traceback.format_exc())
                finally:
                    if download_attempted: # Only remove lock if we attempted to create it
                        try:
                            if os.path.exists(lock_file):
                                os.remove(lock_file)
                        except Exception as e:
                            logger.error(f"[{self.name}] Msg {cmsg.msg_id}: Failed to remove lock file {lock_file}: {e}")
        
        except Exception as e_outer: # Catch errors in the outer XML parsing/setup
            logger.error(f"[{self.name}] Msg {cmsg.msg_id}: Major error in _process_image_message: {e_outer}")
            logger.error(traceback.format_exc())
            # Ensure default attributes if parsing failed badly
            if not hasattr(cmsg, 'img_aeskey'): cmsg.img_aeskey = None
            if not hasattr(cmsg, 'image_info'):
                cmsg.image_info = {'aeskey': '', 'cdnmidimgurl': '', 'length': '0', 'md5': ''}


        # This logging and recent_image_msgs update should happen regardless of download success
        # as the message itself was an image message.
        logger.info(f"[{self.name}] Processed image message (ID:{cmsg.msg_id} From:{cmsg.from_user_id} Sender:{cmsg.sender_wxid})")

        # Record recently received image messages
        # Ensure actual_user_id is set for session_id
        session_user_id = cmsg.actual_user_id if hasattr(cmsg, 'actual_user_id') and cmsg.actual_user_id else cmsg.from_user_id

        # Use self.received_msgs or a dedicated dict for image contexts for plugins
        # self.recent_image_msgs was initialized in __init__
        if hasattr(self, 'recent_image_msgs') and session_user_id:
            self.recent_image_msgs[session_user_id] = cmsg # Store the WX849Message object
            logger.info(f"[{self.name}] Recorded image message context for session {session_user_id} (MsgID: {cmsg.msg_id}).")


        # Final check and update of cmsg properties if image was successfully downloaded and path is set
        if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
            cmsg.content = cmsg.image_path # Update content to be the path
            cmsg.ctype = ContextType.IMAGE # Ensure ctype is IMAGE
            logger.info(f"[{self.name}] Msg {cmsg.msg_id}: Final image path set to: {cmsg.image_path}")
        else:
            logger.warning(f"[{self.name}] Msg {cmsg.msg_id}: Image path not available after processing. Image download might have failed or was skipped.")


    async def _download_image(self, cmsg):
        """下载图片并设置本地路径"""
        try:
            # 检查是否已经有图片路径
            if hasattr(cmsg, 'image_path') and cmsg.image_path and os.path.exists(cmsg.image_path):
                logger.info(f"[WX849] 图片已存在，路径: {cmsg.image_path}")
                return True

            # 创建临时目录
            tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "wx849_img_cache")
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

    async def _download_image_by_chunks(self, cmsg: WX849Message, image_path: str): # Added type hints
        """使用分段下载方法获取图片, 并在成功后缓存."""
        import traceback
        import asyncio
        from io import BytesIO
        from PIL import Image, UnidentifiedImageError
        import shutil # MODIFICATION: Added import for shutil
        import os # Ensure os is imported (likely already is)
        from bridge.context import ContextType # Ensure ContextType is imported

        try:
            # 1. 确保目标目录存在
            target_dir = os.path.dirname(image_path)
            os.makedirs(target_dir, exist_ok=True) # target_dir is self.image_cache_dir

            # 2. 获取API配置及计算分块信息
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            api_port = conf().get("wx849_api_port", 9011) # Assuming 9011 is the media port
            protocol_version = conf().get("wx849_protocol_version", "849")
            api_path_prefix = "/api" if protocol_version in ["855", "ipad"] else "/VXAPI"
            
            data_len_str = '0'
            if hasattr(cmsg, 'image_info') and isinstance(cmsg.image_info, dict):
                data_len_str = cmsg.image_info.get('length', '0')
            elif hasattr(cmsg, 'img_length'):
                data_len_str = cmsg.img_length
            
            try:
                data_len = int(data_len_str)
            except ValueError:
                data_len = 0
            
            if data_len <= 0:
                 logger.warning(f"[{self.name}] Image length is {data_len} from XML for cmsg {cmsg.msg_id}. Download will proceed; actual size determined by API.")

            chunk_size = 65536
            num_chunks = (data_len + chunk_size - 1) // chunk_size if data_len > 0 else 1

            logger.info(f"[{self.name}] 开始分段下载图片 (cmsg_id: {cmsg.msg_id}, aeskey: {getattr(cmsg, 'img_aeskey', 'N/A')}) 至: {image_path}，预期总大小: {data_len if data_len > 0 else 'Unknown'} B，分 {num_chunks} 段 (approx)")

            # 3. 分块下载逻辑
            all_chunks_data_list = []
            download_stream_successful = True
            actual_downloaded_size = 0

            for i in range(num_chunks):
                start_pos = actual_downloaded_size # Use actual_downloaded_size for start_pos
                current_chunk_size = chunk_size # Request a full chunk

                # For the last chunk with known data_len, adjust current_chunk_size if needed.
                if data_len > 0 and (start_pos + chunk_size > data_len):
                    current_chunk_size = data_len - start_pos
                
                if data_len > 0 and current_chunk_size <= 0 and start_pos >= data_len:
                    logger.info(f"[{self.name}] Presumed all data downloaded for cmsg {cmsg.msg_id}. Total downloaded: {start_pos}, Expected: {data_len}. Breaking chunk loop.")
                    break
                if current_chunk_size <= 0 and data_len > 0: # Should not happen if logic above is correct
                    logger.warning(f"[{self.name}] Calculated current_chunk_size as {current_chunk_size} for cmsg {cmsg.msg_id}, but data still expected. Breaking.")
                    download_stream_successful = False; break


                params = {
                    "MsgId": int(cmsg.msg_id),
                    "ToWxid": cmsg.from_user_id, # Sender of the original message
                    "Wxid": self.wxid, # Bot's WXID
                    "DataLen": data_len if data_len > 0 else 0, 
                    "CompressType": 0,
                    "Section": {"StartPos": start_pos, "DataLen": current_chunk_size}
                }
                if hasattr(cmsg, 'img_aeskey') and cmsg.img_aeskey:
                    params["Aeskey"] = cmsg.img_aeskey

                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Tools/DownloadImg"
                logger.debug(f"[{self.name}] 下载分段 {i+1}/{num_chunks} for cmsg {cmsg.msg_id}: URL={api_url}, Params={params}")

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=params, timeout=aiohttp.ClientTimeout(total=30)) as response: # Increased timeout
                            if response.status != 200:
                                full_error_text = await response.text()
                                logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) 失败, HTTP状态码: {response.status}, Response: {full_error_text[:300]}")
                                download_stream_successful = False; break
                            
                            try:
                                result = await response.json()
                            except aiohttp.ContentTypeError:
                                raw_response_text = await response.text()
                                logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) API Error: Non-JSON response. Status: {response.status}. Response text (first 300 chars): {raw_response_text[:300]}")
                                download_stream_successful = False; break

                            # [MODIFIED START] Enhanced API Response Handling
                            api_call_succeeded = False
                            error_message_from_api = "Unknown API error"

                            base_response = result.get("BaseResponse")
                            if isinstance(base_response, dict):
                                api_ret_code = base_response.get("ret")
                                if api_ret_code == 0:
                                    api_call_succeeded = True
                                    # Even if ret is 0, check overall Success flag if API uses it
                                    if not result.get("Success", True): # If Success key exists and is False
                                        api_call_succeeded = False
                                        error_message_from_api = result.get("Message", base_response.get("errMsg", "API Success=false after BaseResponse.ret=0"))
                                elif api_ret_code is not None: # ret is present and non-zero
                                    error_message_from_api = base_response.get("errMsg", f"API error with ret code {api_ret_code}")
                                    logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) API报告错误 (BaseResponse): ret={api_ret_code}, errMsg='{error_message_from_api}'. FullResult: {str(result)[:300]}")
                                    download_stream_successful = False; break
                                else: # BaseResponse exists but no 'ret' field, or 'ret' is None. Fallback to 'Success' flag.
                                    if not result.get("Success", False): # Default to False if Success is missing or explicitly false
                                        error_message_from_api = result.get("Message", "API Success flag is false and BaseResponse.ret is missing.")
                                        logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) API报告失败 (Success flag after missing BaseResponse.ret): {error_message_from_api}. FullResult: {str(result)[:300]}")
                                        download_stream_successful = False; break
                                    else: # BaseResponse without 'ret', but 'Success' is true or missing (implies true)
                                        api_call_succeeded = True 
                            else: # No BaseResponse dict, rely solely on "Success" flag
                                if not result.get("Success", False): # Default to False if Success is missing or explicitly false
                                    error_message_from_api = result.get("Message", "API Success flag is false and no BaseResponse.")
                                    logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) API报告失败 (Success flag, no BaseResponse): {error_message_from_api}. FullResult: {str(result)[:300]}")
                                    download_stream_successful = False; break
                                else: # No BaseResponse, Success is true or missing (implies true by default for this path)
                                    api_call_succeeded = True

                            if not api_call_succeeded: # This should ideally be caught by breaks above, but serves as a final safeguard.
                                # Log if somehow this state is reached without a break.
                                logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) 最终判断为API调用失败. Message: {error_message_from_api}. FullResult: {str(result)[:300]}\")")
                                download_stream_successful = False; break
                            
                            # Proceed with chunk_base64 extraction only if api_call_succeeded
                            chunk_base64 = None
                            # Try to get data from a common "Data" wrapper first
                            data_payload = result.get("Data") # Use .get() to avoid KeyError if "Data" is not present
                            
                            if isinstance(data_payload, dict) and data_payload: # Check if data_payload is a non-empty dict
                                if "buffer" in data_payload and isinstance(data_payload["buffer"], (str, bytes)):
                                    chunk_base64 = data_payload["buffer"]
                                elif "data" in data_payload and isinstance(data_payload.get("data"), dict) and \
                                     "buffer" in data_payload["data"] and isinstance(data_payload["data"]["buffer"], (str, bytes)):
                                    chunk_base64 = data_payload["data"]["buffer"]
                                else: # Check other common fields within data_payload dict
                                    for field in ["Chunk", "Image", "Base64Data", "fileData"]:
                                        potential_data_in_payload = data_payload.get(field)
                                        if isinstance(potential_data_in_payload, (str, bytes)) and potential_data_in_payload:
                                            chunk_base64 = potential_data_in_payload; break
                            elif isinstance(data_payload, str) and data_payload: # "Data" field itself is a base64 string
                                chunk_base64 = data_payload
                            
                            # Fallback: If not found in "Data" payload or "Data" was not a string, check root level fields
                            if not chunk_base64:
                                for field in ["data", "fileData", "Image", "base64Data", "buffer", "chunk"]: # Common root level fields
                                    potential_data_at_root = result.get(field)
                                    if isinstance(potential_data_at_root, (str, bytes)) and potential_data_at_root: # Must be string/bytes and not empty
                                        chunk_base64 = potential_data_at_root; break
                            
                            if not chunk_base64:
                                logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) 成功获取API响应但未能提取到有效的图片数据字符串/字节. Response: {str(result)[:300]}")
                                # If API reported success but no data for the first chunk of an unknown length download, it might be an empty image.
                                if data_len == 0 and i == 0:
                                    logger.info(f"[{self.name}] API reported success but no data for first chunk (unknown length) for cmsg {cmsg.msg_id}. Assuming empty image or end of stream.")
                                    # download_stream_successful can remain true, let it try to finalize as empty.
                                else:
                                     download_stream_successful = False # Mark as failure if data was expected
                                break 
                            # [MODIFIED END]
                            
                            try:
                                if isinstance(chunk_base64, bytes): # If API directly returns bytes
                                    chunk_data_bytes = chunk_base64
                                elif isinstance(chunk_base64, str):
                                    clean_base64 = chunk_base64.strip()
                                    padding_needed = (4 - len(clean_base64) % 4) % 4
                                    clean_base64 += '=' * padding_needed
                                    chunk_data_bytes = base64.b64decode(clean_base64)
                                else:
                                    # This path should not be reached if the extraction logic above correctly ensures chunk_base64 is str or bytes.
                                    logger.error(f"[{self.name}] 逻辑错误: chunk_base64 在解码前既不是字符串也不是字节. Type: {type(chunk_base64)}. Value: {str(chunk_base64)[:100]}")
                                    # This indicates a flaw in the success checking or data extraction logic if it reaches here with a dict.
                                    download_stream_successful = False; break # Treat as critical logic failure.

                                if not chunk_data_bytes and data_len == 0 and i == 0:
                                    logger.info(f"[{self.name}] API returned empty decoded data for chunk 1 (unknown length) for cmsg {cmsg.msg_id}. Download stream ended.")
                                    # download_stream_successful can remain true, all_chunks_data_list will be empty.
                                    break # Break here, as there's no more data.
                                
                                if not chunk_data_bytes and data_len > 0:
                                     logger.warning(f"[{self.name}] Decoded empty chunk {i+1}/{num_chunks} for cmsg {cmsg.msg_id} but expected data.")
                                     # This might be an error or end of data if DataLen was an estimate.
                                     # If API guarantees non-empty chunks until the end, this is an error.
                                     # For now, we'll let it proceed, but it might lead to a smaller file.
                                
                                if chunk_data_bytes: # Only append if there's actual data
                                    all_chunks_data_list.append(chunk_data_bytes)
                                    actual_downloaded_size += len(chunk_data_bytes)
                                    logger.debug(f"[{self.name}] 第 {i+1}/{num_chunks} (cmsg {cmsg.msg_id}) 段解码成功，大小: {len(chunk_data_bytes)} B. Total so far: {actual_downloaded_size} B")
                                else: # No data, but no explicit error from API (e.g. first chunk of unknown length returned empty)
                                    if data_len == 0 and i == 0: # Explicitly break if first chunk for unknown length is empty
                                        break


                            except Exception as decode_err:
                                logger.error(f"[{self.name}] 第 {i+1}/{num_chunks} (cmsg {cmsg.msg_id}) 段Base64解码或处理失败: {decode_err}. Data (头100): {str(chunk_base64)[:100]}")
                                download_stream_successful = False; break
                except asyncio.TimeoutError:
                    logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) 超时。")
                    download_stream_successful = False; break
                except Exception as api_err:
                    logger.error(f"[{self.name}] 下载分段 {i+1} (cmsg {cmsg.msg_id}) 发生API调用错误: {api_err}\n{traceback.format_exc()}")
                    download_stream_successful = False; break
            
            # 4. 数据写入、刷新与同步
            file_written_successfully = False
            if download_stream_successful and all_chunks_data_list: # Ensure there's data to write
                try:
                    # Write to the target image_path (e.g., CACHE_DIR/AESKEY.tmp)
                    with open(image_path, "wb") as f_write:
                        for chunk_piece in all_chunks_data_list:
                            f_write.write(chunk_piece)
                        f_write.flush()
                        if hasattr(os, 'fsync'):
                            try: os.fsync(f_write.fileno())
                            except OSError: pass # Ignore fsync errors on some systems like Windows
                    
                    final_size_on_disk = os.path.getsize(image_path)
                    logger.info(f"[{self.name}] 所有分块成功写入并刷新到磁盘: {image_path}, 实际大小: {final_size_on_disk} B (Downloaded: {actual_downloaded_size} B)")
                    if final_size_on_disk == 0 and actual_downloaded_size > 0 :
                        logger.error(f"[{self.name}] 警告：数据已下载 ({actual_downloaded_size}B) 但写入文件后大小为0！Path: {image_path}")
                        # This is a critical error, file write failed despite no IO Exception.
                        file_written_successfully = False
                    elif final_size_on_disk == 0 and actual_downloaded_size == 0:
                        logger.info(f"[{self.name}] 下载完成，但未收到任何数据且文件大小为0 (可能为空图片或API指示无内容): {image_path}")
                        # This might be acceptable for an empty image. PIL check will confirm.
                        file_written_successfully = True 
                    else:
                        file_written_successfully = True

                except IOError as io_err_write:
                    logger.error(f"[{self.name}] 写入或刷新图片文件失败: {io_err_write}, Path: {image_path}")
                except Exception as e_write:
                    logger.error(f"[{self.name}] 写入文件时发生未知错误: {e_write}, Path: {image_path}\n{traceback.format_exc()}")

            elif not all_chunks_data_list and download_stream_successful:
                logger.warning(f"[{self.name}] 所有分块下载API调用成功，但未收集到任何数据块 for {image_path}. 文件将为空或不存在。")
                # Consider this a failure unless an empty image is valid.
                if os.path.exists(image_path) and os.path.getsize(image_path) == 0:
                    file_written_successfully = True # Empty file was "successfully" written.
                else: # No file or data
                    download_stream_successful = False # Mark as failure for subsequent checks


            # 5. 图片验证阶段 和 重命名 (if image_path was .tmp)
            final_verified_path = None
            if file_written_successfully:
                await asyncio.sleep(0.1) 
                try:
                    if os.path.getsize(image_path) == 0:
                        # Accept empty files if no data was ever downloaded, otherwise it's an error.
                        if actual_downloaded_size == 0:
                            logger.info(f"[{self.name}] Downloaded image file is empty (0 bytes), and 0 bytes were downloaded. Assuming valid empty image: {image_path}")
                             # MODIFICATION START: Handle renaming for empty image using aeskey
                            if hasattr(cmsg, 'img_aeskey') and cmsg.img_aeskey:
                                final_filename_empty_aeskey = f"{cmsg.img_aeskey}.empty" # Or just aeskey if no ext desired for empty
                                final_empty_path_aeskey = os.path.join(target_dir, final_filename_empty_aeskey)
                                try:
                                    if os.path.exists(final_empty_path_aeskey) and final_empty_path_aeskey != image_path: 
                                        os.remove(final_empty_path_aeskey)
                                    os.rename(image_path, final_empty_path_aeskey)
                                    logger.info(f"[{self.name}] Renamed empty image from {image_path} to {final_empty_path_aeskey} using aeskey.")
                                    final_verified_path = final_empty_path_aeskey
                                except OSError as e_rename_empty_aes:
                                    logger.error(f"[{self.name}] Failed to rename empty image {image_path} to use aeskey: {e_rename_empty_aes}")
                                    final_verified_path = image_path # Fallback to original path
                            else: # No aeskey for empty image
                                logger.warning(f"[{self.name}] Empty image downloaded but no cmsg.img_aeskey available for msg {cmsg.msg_id}. Keeping original path: {image_path}")
                                final_verified_path = image_path
                            # MODIFICATION END
                            # Set cmsg for empty image
                            cmsg.image_path = final_verified_path
                            cmsg.content = final_verified_path 
                            cmsg.ctype = ContextType.IMAGE 
                            cmsg._prepared = True
                            return True # Successfully "downloaded" an empty image
                        else: # Size is 0 but data *was* downloaded - this is an error.
                            raise UnidentifiedImageError("Downloaded image file is empty despite data being received.")

                    # Proceed with PIL verification for non-empty files
                    with open(image_path, "rb") as f_read_verify: image_bytes_for_verify = f_read_verify.read()
                    if not image_bytes_for_verify: raise UnidentifiedImageError("Downloaded image file read as empty for verification.")

                    with Image.open(BytesIO(image_bytes_for_verify)) as img:
                        img_format_detected = img.format # This is from PIL
                        img_size = img.size
                    logger.info(f"[{self.name}] 图片(cmsg {cmsg.msg_id})验证成功 (PIL): 格式={img_format_detected}, 大小={img_size}, 初始路径={image_path}")

                    # Determine actual extension using imghdr for more reliability for file system
                    import imghdr # Ensure imported
                    actual_ext_imghdr = imghdr.what(None, h=image_bytes_for_verify) # Pass bytes directly
                    
                    if actual_ext_imghdr:
                        actual_ext = actual_ext_imghdr.lower()
                        if actual_ext == 'jpeg': actual_ext = 'jpg' 
                        logger.info(f"[{self.name}] imghdr detected extension: .{actual_ext} for cmsg {cmsg.msg_id}")
                    elif img_format_detected: 
                        actual_ext = img_format_detected.lower()
                        if actual_ext == 'jpeg': actual_ext = 'jpg'
                        logger.info(f"[{self.name}] imghdr failed, using PIL detected extension: .{actual_ext} for cmsg {cmsg.msg_id}")
                    else:
                        actual_ext = "jpg" 
                        logger.warning(f"[{self.name}] Could not determine image type via PIL or imghdr for cmsg {cmsg.msg_id}. Defaulting to '.jpg'.")

                    # MODIFICATION START: Rename to use aeskey
                    if hasattr(cmsg, 'img_aeskey') and cmsg.img_aeskey:
                        final_filename_aeskey = f"{cmsg.img_aeskey}.{actual_ext}"
                        final_new_path_aeskey = os.path.join(target_dir, final_filename_aeskey)
                        try:
                            if os.path.exists(final_new_path_aeskey) and final_new_path_aeskey != image_path:
                                os.remove(final_new_path_aeskey) 
                            os.rename(image_path, final_new_path_aeskey) # image_path is img_{msg_id}_{timestamp}.jpg
                            logger.info(f"[{self.name}] Renamed cached image from {image_path} to {final_new_path_aeskey} using aeskey.")
                            final_verified_path = final_new_path_aeskey
                        except OSError as e_rename_aes:
                            logger.error(f"[{self.name}] Failed to rename cached image {image_path} to use aeskey {final_new_path_aeskey}: {e_rename_aes}. Using original path.")
                            final_verified_path = image_path 
                    else: # No aeskey, keep original name (img_{msg_id}_{timestamp}.jpg)
                        logger.warning(f"[{self.name}] No cmsg.img_aeskey found for msg {cmsg.msg_id}. Keeping original cache name: {image_path}")
                        final_verified_path = image_path 
                    # MODIFICATION END

                    cmsg.image_path = final_verified_path
                    cmsg.content = final_verified_path 
                    cmsg.ctype = ContextType.IMAGE
                    cmsg._prepared = True
                    return True 

                except UnidentifiedImageError as unident_err:
                    logger.error(f"[{self.name}] 图片验证失败 (PIL无法识别格式) for cmsg {cmsg.msg_id}: {unident_err}, 文件: {image_path}")
                    if os.path.exists(image_path): os.remove(image_path)
                except ImportError: 
                    logger.warning(f"[{self.name}] PIL (Pillow) 或 imghdr 库未安装，无法对图片进行严格验证: {image_path}")
                    fsize = os.path.getsize(image_path) if os.path.exists(image_path) else 0
                    if fsize > 100: 
                        logger.info(f"[{self.name}] 图片下载完成 (无严格验证，大小: {fsize}B)，路径: {image_path}")
                        # MODIFICATION START: Basic rename if no PIL and aeskey exists
                        if hasattr(cmsg, 'img_aeskey') and cmsg.img_aeskey:
                            final_name_no_pil_aes = f"{cmsg.img_aeskey}.jpg" # Default to jpg
                            final_new_path_no_pil_aes = os.path.join(target_dir, final_name_no_pil_aes)
                            try:
                                if os.path.exists(final_new_path_no_pil_aes) and final_new_path_no_pil_aes != image_path: 
                                    os.remove(final_new_path_no_pil_aes)
                                os.rename(image_path, final_new_path_no_pil_aes)
                                final_verified_path = final_new_path_no_pil_aes
                                logger.info(f"[{self.name}] Renamed (no PIL) cached image from {image_path} to {final_verified_path} using aeskey.")
                            except OSError: 
                                final_verified_path = image_path
                                logger.error(f"[{self.name}] Failed to rename (no PIL) cached image {image_path} to use aeskey. Keeping original.")
                        else: 
                            final_verified_path = image_path
                            logger.warning(f"[{self.name}] No PIL and no aeskey. Keeping original name (no PIL): {image_path}")
                        # MODIFICATION END
                        
                        cmsg.image_path = final_verified_path
                        cmsg.content = final_verified_path
                        cmsg.ctype = ContextType.IMAGE
                        cmsg._prepared = True
                        return True
                    else:
                        logger.warning(f"[{self.name}] 无严格验证且文件大小 ({fsize}B) 过小/为0，视为无效: {image_path}")
                        if os.path.exists(image_path): os.remove(image_path)
                except Exception as pil_verify_err: 
                    logger.error(f"[{self.name}] 图片验证时发生未知错误 for cmsg {cmsg.msg_id}: {pil_verify_err}, 文件: {image_path}\n{traceback.format_exc()}")
                    if os.path.exists(image_path): os.remove(image_path)
            
            # 6. 最终失败路径
            logger.error(f"[{self.name}] 图片下载或验证未能成功 for cmsg {cmsg.msg_id} (Path: {image_path}). download_stream_ok={download_stream_successful}, file_written_ok={file_written_successfully}, data_collected={bool(all_chunks_data_list)}.")
            if os.path.exists(image_path):
                try: os.remove(image_path); logger.info(f"[{self.name}] 已删除下载失败或验证失败的图片文件: {image_path}")
                except Exception as e_rm_fail: logger.error(f"[{self.name}] 删除失败的图片文件时出错 {image_path}: {e_rm_fail}")
            
            if cmsg: cmsg._prepared = False
            return False

        except Exception as outer_e: 
            logger.critical(f"[{self.name}] _download_image_by_chunks 发生严重意外错误 for cmsg {cmsg.msg_id}, path {image_path if 'image_path' in locals() else 'Unknown'}: {outer_e}\n{traceback.format_exc()}")
            path_to_clean = image_path if 'image_path' in locals() and os.path.exists(image_path) else None
            if path_to_clean:
                try: os.remove(path_to_clean); logger.info(f"[{self.name}] 意外错误后，已尝试删除图片文件: {path_to_clean}")
                except Exception as e_rm_outer: logger.error(f"[{self.name}] 意外错误后，删除图片文件失败: {e_rm_outer}")
            if cmsg: cmsg._prepared = False
            return False
        
    async def _download_image_with_details(self, image_meta: dict, target_path: str) -> bool:
        """
        Downloads an image using detailed metadata, typically for referenced images.
        Uses chunked download.

        :param image_meta: Dict containing keys like 'msg_id_for_download', 'data_len', 
                           'aeskey', 'downloader_wxid', 'original_sender_wxid'.
        :param target_path: Full path where the image should be saved.
        :return: True if download and verification are successful, False otherwise.
        """
        import traceback
        import asyncio
        from io import BytesIO
        from PIL import Image, UnidentifiedImageError

        logger.info(f"[{self.name}] Attempting download with details: {image_meta} to {target_path}")

        try:
            # 1. Pre-check: Validate target_path and create directory
            tmp_dir = os.path.dirname(target_path)
            os.makedirs(tmp_dir, exist_ok=True)

            # 2. Get API config and calculate chunk info
            api_host = conf().get("wx849_api_host", "127.0.0.1")
            # For image downloads, often a specific media port is used, check if it's configured
            api_port = conf().get("wx849_api_port", conf().get("wx849_api_port", 9011)) 
            protocol_version = conf().get("wx849_protocol_version", "849")
            api_path_prefix = "/api" if protocol_version in ["855", "ipad"] else "/VXAPI"
            
            data_len_str = image_meta.get('data_len', '0')
            try:
                data_len = int(data_len_str)
            except ValueError:
                logger.error(f"[{self.name}] Invalid data_len '{data_len_str}' in image_meta. Using default 0.")
                data_len = 0
            
            if data_len <= 0: # If data_len is 0 or invalid, try a default or log an error
                logger.warning(f"[{self.name}] data_len is {data_len}. Download might be problematic or rely on API to handle it.")
                # Fallback or error handling for zero data_len might be needed depending on API behavior

            chunk_size = 65536  # 64KB
            num_chunks = (data_len + chunk_size - 1) // chunk_size if data_len > 0 else 1
            if data_len == 0 and num_chunks == 1: # Special case for potentially unknown length but expecting at least one chunk
                 logger.info(f"[{self.name}] data_len is 0, attempting to download as a single chunk of default size or as determined by API.")


            logger.info(f"[{self.name}] Downloading referenced image to: {target_path}, Total Size: {data_len} B, Chunks: {num_chunks}")

            # 3. Chunked download logic
            all_chunks_data_list = []
            download_stream_successful = True
            actual_downloaded_size = 0

            for i in range(num_chunks):
                start_pos = i * chunk_size
                current_chunk_size = min(chunk_size, data_len - start_pos) if data_len > 0 else chunk_size # Default to chunk_size if data_len is unknown
                
                if data_len > 0 and current_chunk_size <= 0: # Ensure we don't try to download 0 bytes if data_len was positive
                    logger.debug(f"[{self.name}] Calculated current_chunk_size <=0 with positive data_len. Breaking chunk loop. StartPos: {start_pos}, DataLen: {data_len}")
                    break

                # Ensure msg_id_for_download is an integer for the API call
                msg_id_for_api = None
                try:
                    msg_id_for_api = int(image_meta['msg_id_for_download'])
                except (ValueError, TypeError) as e:
                    logger.error(f"[{self.name}] RefDownload Chunk {i+1} Error: 'msg_id_for_download' ({image_meta.get('msg_id_for_download')}) is not a valid integer: {e}")
                    download_stream_successful = False
                    break

                params = {
                    "MsgId": msg_id_for_api, # MODIFIED: Use the integer version
                    "ToWxid": image_meta.get('original_sender_wxid'), # The user who originally sent the image
                    "Wxid": image_meta.get('downloader_wxid', self.wxid), # The WXID doing the download (our bot)
                    "DataLen": data_len, 
                    "CompressType": 0, 
                    "Section": {"StartPos": start_pos, "DataLen": current_chunk_size}
                }
                # Add aeskey if present and non-empty
                if image_meta.get('aeskey'):
                    params["Aeskey"] = image_meta['aeskey']

                api_url = f"http://{api_host}:{api_port}{api_path_prefix}/Tools/DownloadImg"
                logger.debug(f"[{self.name}] RefDownload Chunk {i+1}/{num_chunks}: URL={api_url}, Params={params}")

                try:
                    async with aiohttp.ClientSession() as session:
                        # Increased timeout for potentially slow media downloads
                        async with session.post(api_url, json=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                            if response.status != 200:
                                full_error_text = await response.text()
                                logger.error(f"[{self.name}] RefDownload Chunk {i+1} HTTP Error: {response.status}, Response: {full_error_text[:500]}")
                                download_stream_successful = False
                                break
                            
                            try:
                                result = await response.json()
                            except aiohttp.ContentTypeError:
                                raw_response_text = await response.text()
                                logger.error(f"[{self.name}] RefDownload Chunk {i+1} API Error: Non-JSON response. Status: {response.status}. Response text (first 500 chars): {raw_response_text[:500]}")
                                download_stream_successful = False
                                break
                            
                            if not result or not isinstance(result, dict):
                                logger.error(f"[{self.name}] RefDownload Chunk {i+1} API Error: Invalid or empty JSON response. FullResult: {result}")
                                download_stream_successful = False
                                break

                            if not result.get("Success", False):
                                logger.error(f"[{self.name}] RefDownload Chunk {i+1} API Error: {result.get('Message', 'Unknown API error')}, FullResult: {result}")
                                download_stream_successful = False
                                break
                            
                            data_payload = result.get("Data", {})
                            chunk_base64 = None
                            if isinstance(data_payload, dict):
                                if "buffer" in data_payload: chunk_base64 = data_payload["buffer"]
                                elif "data" in data_payload and isinstance(data_payload.get("data"), dict) and "buffer" in data_payload["data"]: chunk_base64 = data_payload["data"]["buffer"]
                                else: 
                                    for field in ["Chunk", "Image", "Data", "FileData"]: # Common field names
                                        if field in data_payload: chunk_base64 = data_payload.get(field); break
                            elif isinstance(data_payload, str): # Direct base64 string
                                chunk_base64 = data_payload
                            
                            if not chunk_base64 and isinstance(result, dict): # Fallback to check root result
                                 for field in ["data", "Data", "FileData", "Image"]:
                                     if field in result and result.get(field): chunk_base64 = result.get(field); break

                            if not chunk_base64:
                                logger.error(f"[{self.name}] RefDownload Chunk {i+1} Error: No image data found in API response. Response: {str(result)[:200]}")
                                download_stream_successful = False
                                break
                            
                            try:
                                if not isinstance(chunk_base64, str):
                                    if isinstance(chunk_base64, bytes):
                                        try: chunk_base64 = chunk_base64.decode('utf-8')
                                        except UnicodeDecodeError: raise ValueError("chunk_base64 is bytes but cannot be utf-8 decoded.")
                                    else: raise ValueError(f"chunk_base64 is not str or bytes: {type(chunk_base64)}")
                                
                                clean_base64 = chunk_base64.strip()
                                padding = (4 - len(clean_base64) % 4) % 4
                                clean_base64 += '=' * padding
                                chunk_data_bytes = base64.b64decode(clean_base64)
                                all_chunks_data_list.append(chunk_data_bytes)
                                actual_downloaded_size += len(chunk_data_bytes)
                                logger.debug(f"[{self.name}] RefDownload Chunk {i+1}/{num_chunks} decoded, size: {len(chunk_data_bytes)} B. Total so far: {actual_downloaded_size} B")
                            except Exception as decode_err:
                                logger.error(f"[{self.name}] RefDownload Chunk {i+1}/{num_chunks} Base64 decode error: {decode_err}. Data (first 100): {str(chunk_base64)[:100]}")
                                download_stream_successful = False
                                break
                except asyncio.TimeoutError:
                    logger.error(f"[{self.name}] RefDownload Chunk {i+1} timed out.")
                    download_stream_successful = False
                    break
                except Exception as api_call_err:
                    logger.error(f"[{self.name}] RefDownload Chunk {i+1} API call error: {api_call_err}\n{traceback.format_exc()}")
                    download_stream_successful = False
                    break
            
            # 4. Data writing, flushing, and syncing
            file_written_successfully = False
            if download_stream_successful and all_chunks_data_list:
                try:
                    with open(target_path, "wb") as f_write:
                        for chunk_piece in all_chunks_data_list:
                            f_write.write(chunk_piece)
                        f_write.flush()
                        if hasattr(os, 'fsync'): # fsync might not be available on all OS (e.g. some Windows setups)
                            try:
                                os.fsync(f_write.fileno())
                            except OSError as e_fsync:
                                logger.warning(f"[{self.name}] os.fsync failed for {target_path}: {e_fsync}. Continuing without fsync.")
                        else:
                            logger.debug(f"[{self.name}] os.fsync not available on this system.")

                    final_file_size = os.path.getsize(target_path)
                    logger.info(f"[{self.name}] RefDownload: All chunks written to disk: {target_path}, Actual Final Size: {final_file_size} B (Expected: {data_len} B, Downloaded: {actual_downloaded_size} B)")
                    if final_file_size == 0 and actual_downloaded_size > 0:
                        logger.error(f"[{self.name}] RefDownload WARNING: Data downloaded ({actual_downloaded_size}B) but written file size is 0! Path: {target_path}")
                    else:
                        file_written_successfully = True
                except IOError as io_err_write_final:
                    logger.error(f"[{self.name}] RefDownload: Failed to write or flush image file: {io_err_write_final}, Path: {target_path}")
                except Exception as e_write_final:
                    logger.error(f"[{self.name}] RefDownload: Unknown error during file write: {e_write_final}, Path: {target_path}\n{traceback.format_exc()}")
            elif not all_chunks_data_list and download_stream_successful:
                logger.warning(f"[{self.name}] RefDownload: API calls successful, but no data chunks collected for {target_path}.")
            
            # 5. Image Verification Stage
            if file_written_successfully:
                await asyncio.sleep(0.1) # Brief pause to ensure file system operations complete
                try:
                    with open(target_path, "rb") as f_read_verify_final:
                        image_bytes_for_verify_final = f_read_verify_final.read()
                    
                    if not image_bytes_for_verify_final:
                        logger.error(f"[{self.name}] RefDownload: Image file empty after download and read for verification: {target_path}")
                        raise UnidentifiedImageError("Downloaded image file is empty for verification.")

                    with Image.open(BytesIO(image_bytes_for_verify_final)) as img_final:
                        img_format_final = img_final.format
                        img_size_final = img_final.size
                        logger.info(f"[{self.name}] RefDownload: Image verification successful (PIL): Format={img_format_final}, Size={img_size_final}, Path={target_path}")
                        return True
                except UnidentifiedImageError as unident_err_final:
                    logger.error(f"[{self.name}] RefDownload: Image verification failed (PIL UnidentifiedImageError): {unident_err_final}, File: {target_path}")
                    if os.path.exists(target_path): os.remove(target_path)
                    return False
                except ImportError: # Should have been caught earlier, but as a safeguard
                    logger.warning("[WX849] RefDownload: PIL (Pillow) library not installed, cannot perform strict image verification.")
                    fsize_final_no_pil = os.path.getsize(target_path) if os.path.exists(target_path) else 0
                    if fsize_final_no_pil > 1000: # Heuristic: >1KB might be a valid small image
                        logger.info(f"[{self.name}] RefDownload: Image download likely complete (No PIL verification, size: {fsize_final_no_pil}B), Path: {target_path}")
                        return True
                    else:
                        logger.warning(f"[{self.name}] RefDownload: PIL not installed AND file size ({fsize_final_no_pil}B) is too small. Invalid: {target_path}")
                        if os.path.exists(target_path): os.remove(target_path)
                        return False
                except Exception as pil_verify_err_final:
                    logger.error(f"[{self.name}] RefDownload: Unknown PIL verification error: {pil_verify_err_final}, File: {target_path}\n{traceback.format_exc()}")
                    if os.path.exists(target_path): os.remove(target_path)
                    return False
            
            # 6. Final Failure Path (if not returned True already)
            logger.error(f"[{self.name}] RefDownload: Image download or verification failed. StreamOK={download_stream_successful}, WrittenOK={file_written_successfully}, DataCollected={bool(all_chunks_data_list)}. Path: {target_path}")
            if os.path.exists(target_path): # Cleanup if file exists but process failed
                try:
                    os.remove(target_path)
                    logger.info(f"[{self.name}] RefDownload: Deleted failed/unverified image file: {target_path}")
                except Exception as e_remove_cleanup:
                    logger.error(f"[{self.name}] RefDownload: Error deleting failed image file: {e_remove_cleanup}, Path: {target_path}")
            return False

        except Exception as outer_e_details:
            logger.critical(f"[{self.name}] _download_image_with_details: Critical unexpected error: {outer_e_details}\n{traceback.format_exc()}")
            path_to_cleanup_outer = target_path
            if path_to_cleanup_outer and os.path.exists(path_to_cleanup_outer):
                try: os.remove(path_to_cleanup_outer)
                except Exception as e_remove_critical: logger.error(f"[{self.name}] Critical error: Failed to cleanup {path_to_cleanup_outer}: {e_remove_critical}")
            return False

    def _get_image(self, msg_id):
        """获取图片数据"""
        # 查找图片文件
        tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tmp", "wx849_img_cache")

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

    def _process_xml_message(self, cmsg: WX849Message):
        """
        处理 XML 类型的消息，主要是 Type 57 引用和 Type 5 分享链接。
        会修改 cmsg 的 ctype 和 content 属性。
        """
        import xml.etree.ElementTree as ET
        import re 
        import asyncio 
        import os 
        import time 
        import traceback
        import tempfile 
        import threading 
        from bridge.context import ContextType 

        try:
            msg_xml = ET.fromstring(cmsg.content)
            appmsg = msg_xml.find("appmsg")

            # 1. 处理引用消息 (Type 57)
            if appmsg is not None and appmsg.findtext("type") == "57":
                refermsg = appmsg.find("refermsg")
                if refermsg is not None:
                    refer_type = refermsg.findtext("type")
                    title = appmsg.findtext("title") # User's question part / command
                    displayname = refermsg.findtext("displayname") # Quoter's display name

                    # 1.1 处理文本引用 (refermsg type=1)
                    if refer_type == "1":
                        quoted_text = refermsg.findtext("content")
                        if title and displayname and quoted_text:
                            prompt = (
                                f"用户针对以下消息提问：\"{title}\"\n\n"
                                f"被引用的消息来自\"{displayname}\"：\n\"{quoted_text}\"\n\n"
                                f"请基于被引用的消息回答用户的问题。"
                            )
                            cmsg.content = prompt
                            cmsg.is_processed_text_quote = True 
                            cmsg.ctype = ContextType.TEXT 
                            logger.info(f"[{self.name}] Processed text quote msg {cmsg.msg_id}. Set type to TEXT.")
                            return 

                    # 1.2 处理聊天记录引用 (refermsg type=49 -> inner appmsg type=19)
                    elif refer_type == "49":
                        quoted_content_raw = refermsg.findtext("content")
                        if quoted_content_raw:
                            try:
                                inner_xml_root = ET.fromstring(quoted_content_raw)
                                inner_appmsg = inner_xml_root.find("appmsg")
                                if inner_appmsg is not None and inner_appmsg.findtext("type") == "19": 
                                    chat_record_desc = inner_appmsg.findtext("des") 
                                    if title and displayname and chat_record_desc:
                                        prompt = (
                                            f"用户针对以下聊天记录提问：\"{title}\"\n\n"
                                            f"被引用的聊天记录来自\"{displayname}\"：\n（摘要：{chat_record_desc}）\n\n"
                                            f"请基于被引用的聊天记录内容回答用户的问题（注意：聊天记录可能包含多条消息）。"
                                        )
                                        cmsg.content = prompt
                                        cmsg.is_processed_text_quote = True 
                                        cmsg.ctype = ContextType.TEXT
                                        logger.info(f"[{self.name}] Processed chat record quote msg {cmsg.msg_id}. Set type to TEXT.")
                                        return 
                            except ET.ParseError:
                                logger.debug(f"[{self.name}] Inner XML parsing failed for type 49 refermsg content in msg {cmsg.msg_id}")
                            except Exception as e_inner:
                                logger.warning(f"[{self.name}] Error processing inner XML for type 49 refermsg in msg {cmsg.msg_id}: {e_inner}")
                    
                    # MODIFICATION START: Handling for referenced image (refer_type == '3' implied by finding 'img' node)
                    elif refer_type == "3" and (quoted_content_raw := refermsg.findtext("content")): 
                        # This 'elif' specifically targets Type 3 (image) references if an explicit check for refer_type is desired.
                        # The original code relied on finding an 'img' node within the quoted_content_raw.
                        
                        original_image_svrid = refermsg.findtext("svrid") # Still useful for logging/context
                        
                        try:
                            inner_xml_root = ET.fromstring(quoted_content_raw)
                            img_node = inner_xml_root.find("img")

                            if img_node is not None:
                                extracted_refer_aeskey = img_node.get("aeskey")
                                # title and displayname are already defined above for Type 57

                                if extracted_refer_aeskey and hasattr(self, 'image_cache_dir') and self.image_cache_dir:
                                    logger.debug(f"[{self.name}] Msg {cmsg.msg_id} (Type 57 quote) references image with aeskey: {extracted_refer_aeskey}. User command: '{title}'. Original svrid: {original_image_svrid}")
                                    
                                    found_cached_path = None
                                    # Try common extensions or the extension determined during caching
                                    # Assuming caching logic (Phase A3) saves with original extension or defaults to .jpg
                                    # Let's try common ones, prioritising .jpg
                                    possible_extensions = ['.jpg', '.jpeg', '.png', '.gif'] 
                                    # A more robust way would be to store file extension along with aeskey if it can vary
                                    # or ensure a consistent extension like .jpg during caching.
                                    
                                    for ext in possible_extensions:
                                        # Ensure consistent naming with caching logic (Phase A3)
                                        # Example: cached_file_name = f"{cmsg.img_aeskey}{file_extension}"
                                        potential_path = os.path.join(self.image_cache_dir, f"{extracted_refer_aeskey}{ext}")
                                        if os.path.exists(potential_path):
                                            found_cached_path = potential_path
                                            break
                                    
                                    if found_cached_path:
                                        logger.info(f"[{self.name}] Found cached image for aeskey {extracted_refer_aeskey} at {found_cached_path} for msg {cmsg.msg_id}")
                                        
                                        cmsg.content = title if title else "" 
                                        cmsg.ctype = ContextType.TEXT
                                        cmsg.original_user_question = title if title else "" 
                                        cmsg.referenced_image_path = found_cached_path
                                        cmsg.is_processed_image_quote = True 
                                        
                                        if displayname:
                                            cmsg.quoter_display_name = displayname
                                        cmsg.quoted_image_id = extracted_refer_aeskey # Using aeskey as a quoted image identifier
                                        
                                        logger.info(f"[{self.name}] Successfully processed referenced image (from cache) for msg {cmsg.msg_id}. Set ctype=TEXT. Path: {cmsg.referenced_image_path}")
                                        return # Crucial: stop further processing of this XML
                                    else:
                                        logger.warning(f"[{self.name}] Referenced image with aeskey {extracted_refer_aeskey} not found in cache ({self.image_cache_dir}) for msg {cmsg.msg_id}. Fallback: No API download configured for this path.")
                                        # If you had a working API download as fallback, it would go here.
                                        # For now, if not in cache, it will be treated as an unhandled Type 57 quote.

                                else: # extracted_refer_aeskey is None or image_cache_dir not set
                                    if not extracted_refer_aeskey:
                                        logger.warning(f"[{self.name}] Referenced image in msg {cmsg.msg_id} has no aeskey in its XML, cannot look up in cache.")
                                    if not (hasattr(self, 'image_cache_dir') and self.image_cache_dir):
                                         logger.error(f"[{self.name}] Image cache directory not configured. Cannot look up referenced image for msg {cmsg.msg_id}")

                            # else: img_node was None (not an image reference within the content)
                            # This case would also fall through.

                        except ET.ParseError as e_parse_inner:
                            logger.debug(f"[{self.name}] Failed to parse inner XML for referenced msg content in msg {cmsg.msg_id}: {e_parse_inner}. Content: {quoted_content_raw[:100] if quoted_content_raw else 'None'}")
                        except Exception as e_proc_ref_img: 
                            logger.error(f"[{self.name}] Error processing potential image reference in msg {cmsg.msg_id}: {e_proc_ref_img}\n{traceback.format_exc()}")
                    # MODIFICATION END
                    
                    # Fallback for unhandled Type 57 messages (if not text, chat record, or successfully processed image quote from cache)
                    if not (hasattr(cmsg, 'is_processed_text_quote') and cmsg.is_processed_text_quote or \
                            hasattr(cmsg, 'is_processed_image_quote') and cmsg.is_processed_image_quote):
                        logger.debug(f"[{self.name}] Unhandled Type 57 refermsg (type='{refer_type}') in msg {cmsg.msg_id}. Title: '{title}'. Will be treated as generic XML.")
                        if title:
                             cmsg.content = f"用户引用了一个消息并提问：\"{title}\" (类型：{refer_type}，未特殊处理)"
                        else:
                             cmsg.content = f"用户引用了一个未处理类型的消息 (类型：{refer_type})"
                        cmsg.ctype = ContextType.XML 

            elif appmsg is not None and appmsg.findtext("type") == "5":
                url = appmsg.findtext("url")
                link_title = appmsg.findtext("title") 
                if url:
                    if not url.startswith("http"):
                        url = "http:" + url if url.startswith("//") else "http://" + url
                    if "." in url and " " not in url: # Basic URL validation
                        cmsg.content = url 
                        cmsg.ctype = ContextType.SHARING 
                        logger.info(f"[{self.name}] Processed sharing link msg {cmsg.msg_id}. URL: {url}, Title: {link_title}")
                        return 
                    else:
                         logger.warning(f"[{self.name}] Invalid URL extracted from sharing link msg {cmsg.msg_id}: {url}")
                else:
                    logger.warning(f"[{self.name}] Sharing link msg {cmsg.msg_id} has no URL.")
            
            # Check if any processing flag was set or if it's a sharing link
            processed_flags_true = (hasattr(cmsg, 'is_processed_text_quote') and cmsg.is_processed_text_quote) or \
                                   (hasattr(cmsg, 'is_processed_image_quote') and cmsg.is_processed_image_quote)
            is_sharing_link = hasattr(cmsg, 'ctype') and cmsg.ctype == ContextType.SHARING

            if not (processed_flags_true or is_sharing_link):

                if appmsg is not None: # Only default to XML if it was an appmsg
                    cmsg.ctype = ContextType.XML 
                    logger.debug(f"[{self.name}] XML message {cmsg.msg_id} (appmsg type: {appmsg.findtext('type') if appmsg is not None else 'N/A'}) not specifically processed. Final ctype={cmsg.ctype}.")
                # else: If not an appmsg, its ctype should have been determined earlier or it's not XML.
        
        except ET.ParseError: # Error parsing the main cmsg.content
            logger.debug(f"[{self.name}] Failed to parse content as XML for msg {cmsg.msg_id}. Content: {str(cmsg.content)[:200]}... Assuming not XML or malformed.")
            # Do not return here, let it fall through. If ctype not set, it might be handled by caller.
            # Or, if it's guaranteed to be XML if this method is called, then this is an error state.
            pass


        except Exception as e:
            logger.error(f"[{self.name}] Unexpected error processing XML message {cmsg.msg_id}: {e}\n{traceback.format_exc()}")
            # Fallback ctype if an unexpected error occurs
            if not hasattr(cmsg, 'ctype') or cmsg.ctype == ContextType.XML: # Avoid overriding if already set to TEXT etc.
                 cmsg.ctype = ContextType.TEXT # Default to TEXT to show error to user potentially
                 cmsg.content = "[XML消息处理时发生内部错误]"
            return # Return on unhandled exception to prevent further issues

        # Group message sender processing - this seems out of place if msg_xml parsing failed.
        # This should ideally be higher up or only if msg_xml was successfully parsed.
        # However, to match the original structure provided:
        if msg_xml is not None and cmsg.is_group and not (hasattr(cmsg, 'actual_user_id') and cmsg.actual_user_id):
            try:
                 # 'fromusername' is usually on the root <msg> for group messages if it's the raw XML
                 sender_id_xml = msg_xml.get('fromusername') 
                 if sender_id_xml:
                     cmsg.sender_wxid = sender_id_xml # This might be the group ID itself
                     cmsg.actual_user_id = sender_id_xml # This needs to be the actual sender in group
                     logger.debug(f"[{self.name}] Attempted to extract sender_wxid '{sender_id_xml}' from group XML msg {cmsg.msg_id}")
                     # This logic for group sender needs careful review based on actual XML structure for group messages.
                     # Often, for group messages, the sender is in a different field or part of a CDATA section.
            except Exception as e_sender:
                logger.error(f"[{self.name}] Error extracting sender from group XML msg {cmsg.msg_id}: {e_sender}")
        
        processed_text_quote_status = getattr(cmsg, 'is_processed_text_quote', False)
        processed_image_quote_status = getattr(cmsg, 'is_processed_image_quote', False)
        current_ctype = getattr(cmsg, 'ctype', 'Unknown') # Default to 'Unknown' if not set
        logger.debug(f"[{self.name}] Finished _process_xml_message for {cmsg.msg_id}. Final ctype={current_ctype}, is_text_quote={processed_text_quote_status}, is_image_quote={processed_image_quote_status}")

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

    def _is_likely_base64_for_log(self, s: str) -> bool:
        """
        判断字符串是否可能是base64编码 (用于日志记录目的)。
        直接改编自 gemini_image.py 中的 _is_likely_base64。
        """
        if not isinstance(s, str): # 确保是字符串
            return False
        # base64编码通常只包含A-Z, a-z, 0-9, +, /, =
        if not s or len(s) < 50:  # 太短的字符串不太可能是需要截断的base64
            return False
            
        # 检查字符是否符合base64编码
        base64_chars_set = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
        non_base64_count = 0
        for char_value in s: # s 是字符串，char_value 是字符
            if char_value not in base64_chars_set and char_value != '=': # '=' 是填充字符
                non_base64_count += 1
        
        if non_base64_count < len(s) * 0.05 and len(s) > 100:
            return True
        return False

    def _create_loggable_params(self, data: any) -> any:
        """
        创建参数的安全版本，用于日志记录。
        将可能的base64数据替换为长度和预览指示器。
        此函数通过构建新的字典/列表来确保原始数据不被修改。
        """
        if isinstance(data, dict):
            new_dict = {}
            for key, value in data.items():
                new_dict[key] = self._create_loggable_params(value) # 递归调用
            return new_dict
        elif isinstance(data, list):
            new_list = []
            for item in data:
                new_list.append(self._create_loggable_params(item)) # 递归调用
            return new_list
        elif isinstance(data, bytes): # <--- 新增对 bytes 类型的处理
            return f"<binary_bytes_data len={len(data)} bytes>"
        elif isinstance(data, str):
            if self._is_likely_base64_for_log(data):
                # 截断并添加长度指示器，类似 gemini_image.py 的做法
                return f"{data[:20]}... [base64_len:{len(data)} chars]"
            else:
                return data # 如果不是base64或太短，返回原字符串
        else:
            # 对于其他数据类型 (如 int, float, bool, None 等) 返回原样
            return data

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

            loggable_params = self._create_loggable_params(params)
            logger.debug(f"[WX849] 请求参数: {json.dumps(loggable_params, ensure_ascii=False)}")
            
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

            elif isinstance(image_source, bytes):
                # 处理bytes对象
                logger.debug("[WX849] 处理 bytes 类型的图片源")
                image_data = image_source
                if not image_data:
                    logger.error("[WX849] 发送图片失败: bytes 对象为空")
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

    async def _prepare_video_and_thumb(self, video_url: str, session_id: str) -> dict:
        """
        异步下载视频，提取缩略图和时长。
        返回包含 video_path, thumb_path, duration 的字典，失败则返回 None。
        """
        tmp_dir = TmpDir().path()
        # 使用 uuid 生成更独特的文件名，避免仅依赖 session_id 和时间戳可能产生的冲突
        unique_id = str(uuid.uuid4())
        video_file_name = f"tmp_video_{session_id}_{unique_id}.mp4" # 假设是mp4
        video_file_path = os.path.join(tmp_dir, video_file_name)
        thumb_file_name = f"tmp_thumb_{session_id}_{unique_id}.jpg"
        thumb_file_path = os.path.join(tmp_dir, thumb_file_name)

        video_downloaded = False
        try:
            # 1. 异步下载视频
            async with aiohttp.ClientSession() as session:
                async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp: # 60秒超时
                    if resp.status == 200:
                        with open(video_file_path, 'wb') as f:
                            while True:
                                chunk = await resp.content.read(1024) # 读取块
                                if not chunk:
                                    break
                                f.write(chunk)
                        video_downloaded = True
                        logger.debug(f"[WX849] Video downloaded to {video_file_path} from {video_url}")
                    else:
                        logger.error(f"[WX849] Failed to download video from {video_url}. Status: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"[WX849] Exception during video download from {video_url}: {e}", exc_info=True)
            if os.path.exists(video_file_path): # 如果下载部分成功但后续出错，清理掉
                os.remove(video_file_path)
            return None

        if not video_downloaded:
            return None

        # 2. 使用 OpenCV 处理视频，提取缩略图和时长
        duration = 0
        thumb_generated = False
        cap = None # 初始化 cap
        try:
            cap = cv2.VideoCapture(video_file_path)
            if not cap.isOpened():
                logger.error(f"[WX849] OpenCV could not open video file: {video_file_path}")
                # 不需要在这里删除 video_file_path，send_video 的 finally 会处理
                return {"video_path": video_file_path, "thumb_path": None, "duration": 0} # 返回部分信息

            # 获取时长
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if fps > 0 and frame_count > 0:
                duration = int(frame_count / fps)
            else:
                logger.warning(f"[WX849] Could not get valid fps ({fps}) or frame_count ({frame_count}) for {video_file_path}. Duration set to 0.")
                duration = 0 # 或设置为一个默认值，或标记为未知

            # 提取第一帧作为缩略图
            ret, frame = cap.read()
            if ret:
                # 有些视频可能需要旋转，这里暂不处理旋转，直接保存帧
                # 可以考虑Pillow进行更精细的图片处理和保存，但cv2.imwrite通常足够
                cv2.imwrite(thumb_file_path, frame) 
                thumb_generated = True
                logger.debug(f"[WX849] Thumbnail generated for {video_file_path} at {thumb_file_path}. Duration: {duration}s")
            else:
                logger.warning(f"[WX849] Could not read frame from video {video_file_path} to generate thumbnail.")
        
        except Exception as e:
            logger.error(f"[WX849] Exception during OpenCV video processing for {video_file_path}: {e}", exc_info=True)
            # 即使处理失败，视频已下载，返回视频路径和获取到的时长（可能为0）
            # thumb_path 将为 None（如果之前未成功生成）
            # 不需要在这里删除 video_file_path，send_video 的 finally 会处理
            return {"video_path": video_file_path, "thumb_path": None, "duration": duration}
        finally:
            if cap:
                cap.release()
        
        return {
            "video_path": video_file_path,
            "thumb_path": thumb_file_path if thumb_generated else None,
            "duration": duration
        }

    async def send_video(self, to_wxid: str, video_url: str, session_id: str):
        """
        下载视频URL，准备视频路径、缩略图路径和时长，
        然后调用 self.api_client.send_video_message 发送。
        """
        logger.info(f"[WX849] Preparing video from URL: {video_url} for recipient {to_wxid} (session: {session_id})")
        prepared_video_info = await self._prepare_video_and_thumb(video_url, session_id)

        if not prepared_video_info or not prepared_video_info.get("video_path"):
            logger.error(f"[WX849] Failed to prepare video and thumbnail for URL: {video_url}")
            return None 

        video_path = prepared_video_info["video_path"]
        thumb_path = prepared_video_info.get("thumb_path") # May be None if fallback is used or generation failed
        # duration = prepared_video_info.get("duration", 0) # api_client.send_video_message will recalculate duration

        if not os.path.exists(video_path): # Double check, _prepare_video_and_thumb should ensure this
            logger.error(f"[WX849] Prepared video file does not exist after _prepare_video_and_thumb: {video_path}")
            return None

        # thumb_path can be None, send_video_message handles a None image by using a fallback.
        # If thumb_path is provided but doesn't exist, it's an issue.
        if thumb_path and not os.path.exists(thumb_path):
            logger.warning(f"[WX849] Prepared thumbnail file does not exist: {thumb_path}. Passing None to API client.")
            thumb_path = None 

        try:
            logger.info(f"[WX849] Calling api_client.send_video_message. ToWxid: {to_wxid}, VideoPath: {video_path}, ThumbPath: {thumb_path if thumb_path else 'Default'}")
            
            # self.api_client 应该是指向 WechatAPI.Client 的实例
            if hasattr(self.bot, 'send_video_message'): # <--- 修改点 1
                # --- BEGIN MODIFICATION ---
                video_path_obj = Path(video_path) # video_path 此时必定存在，前面有检查

                # 只有当 thumb_path 存在且是一个有效文件时才创建 Path 对象，否则为 None
                image_path_obj = Path(thumb_path) if thumb_path and os.path.exists(thumb_path) else None
                
                # 再次确认 video_path_obj 是否有效 (虽然 _prepare_video_and_thumb 和前面的检查应该保证了)
                if not os.path.exists(video_path_obj):
                    logger.error(f"[WX849] Video path object {video_path_obj} does not exist before API call.")
                    return {"Success": False, "Msg": f"Video path {video_path_obj} vanished."}

                result_tuple = await self.bot.send_video_message(
                    wxid=to_wxid, 
                    video=video_path_obj,  # 传递 Path 对象
                    image=image_path_obj   # 传递 Path 对象或 None
                )
                # --- END MODIFICATION ---
                
                if result_tuple and isinstance(result_tuple, tuple) and len(result_tuple) == 2:
                    logger.info(f"[WX849] Video sent successfully via self.bot.send_video_message to {to_wxid}. ClientMsgId: {result_tuple[0]}, NewMsgId: {result_tuple[1]}")
                    result = {"Success": True, "Data": {"clientMsgId": result_tuple[0], "newMsgId": result_tuple[1]}, "Msg": "Sent successfully"}
                else:
                    logger.error(f"[WX849] self.bot.send_video_message to {to_wxid} did not return expected tuple or may have failed silently. Response: {result_tuple}")
                    result = {"Success": False, "Data": result_tuple, "Msg": "API call may have failed or returned unexpected data."}
            else:
                logger.error("[WX849] self.bot does not have send_video_message method.") # <--- 修改点 3
                result = {"Success": False, "Msg": "Bot object is missing send_video_message method."}
            
            return result

        except Exception as e:
            # 如果 self.api_client.send_video_message 内部的 error_handler 抛出异常，这里会捕获
            logger.error(f"[WX849] Exception when calling api_client.send_video_message for {to_wxid}: {e}", exc_info=True)
            return {"Success": False, "Msg": str(e)} # 返回包含错误信息的字典
        finally:
            # 清理 _prepare_video_and_thumb 创建的临时文件
            if os.path.exists(video_path):
                try:
                    os.remove(video_path)
                    logger.debug(f"[WX849] Cleaned up temp video file: {video_path}")
                except Exception as e_clean:
                    logger.warning(f"[WX849] Failed to clean up temp video file {video_path}: {e_clean}")
            if thumb_path and os.path.exists(thumb_path): # 只有当 thumb_path 不是 None 且实际存在时才尝试删除
                try:
                    os.remove(thumb_path)
                    logger.debug(f"[WX849] Cleaned up temp thumb file: {thumb_path}")
                except Exception as e_clean:
                    logger.warning(f"[WX849] Failed to clean up temp thumb file {thumb_path}: {e_clean}")

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
            logger.info(f"[WX849] Received VIDEO_URL reply: {reply.content}")
            to_wxid = context.get("receiver") # 使用 .get() 更安全
            if not to_wxid:
                logger.error("[WX849] Cannot send VIDEO_URL, receiver is not defined in context.")
                return # 如果没有接收者，则返回

            session_id = context.get("session_id") or context.get("msg", {}).get("msg_id") or self.get_random_session()
            # 下面的 if not session_id 理论上不会执行，因为 or self.get_random_session() 保证了它有值
            # 可以考虑移除这个 if 块，或者保留作为额外的日志点
            if not session_id: # 这个判断在 or self.get_random_session() 后其实是多余的
                session_id = self.get_random_session() # 这一行不会被执行
                logger.warning(f"[WX849] session_id was unexpectedly still None for VIDEO_URL, using random: {session_id}")

            try:
                # loop 变量在 send 方法的开头定义
                loop.run_until_complete(self.send_video(to_wxid, reply.content, session_id))
            except Exception as e:
                # send_video 内部已有详细日志，这里可以简化或根据需要调整
                logger.error(f"[WX849] Error occurred in send_reply while processing VIDEO_URL: {str(e)}")
                # 决定是否重新抛出异常
                # raise 
            
            return
        
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

            try:
                # 手动触发 ON_RECEIVE_MESSAGE 事件
                e_context = EventContext(Event.ON_RECEIVE_MESSAGE, {"channel": self, "context": context})
                PluginManager().emit_event(e_context)
                context = e_context["context"] # 获取可能被修改的 context

                # 检查插件是否阻止了消息 或 清空了 context
                if e_context.is_pass() or context is None:
                    logger.info(f"[WX849] Event ON_RECEIVE_MESSAGE breaked or context is None by plugin {e_context.get('breaked_by', 'N/A')}. Returning early.")
                    return context # 返回 None 或被插件修改的 context
            except Exception as plugin_e:
                logger.error(f"[WX849] Error during ON_RECEIVE_MESSAGE event processing: {plugin_e}", exc_info=True)
                # 根据需要决定是否继续，这里选择继续返回原始 context
            # --- 结束插入修改 ---

            return context # 返回（可能被插件修改过的）context
        except Exception as e:
            # ... (原有的错误处理 L4875-L4878) ...
            logger.error(f"[WX849] 构建上下文失败: {e}")
            logger.error(f"[WX849] 详细错误: {traceback.format_exc()}")
            return None
