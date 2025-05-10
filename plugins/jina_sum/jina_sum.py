# encoding:utf-8
import json
import os
import html
from urllib.parse import urlparse, quote
import time
import re
import random

import requests
import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *

# 默认认为requests已安装，因为它是基本依赖
has_requests = True

# 尝试导入BeautifulSoup和requests_html，用于高级内容提取
try:
    from bs4 import BeautifulSoup
    has_bs4 = True
except ImportError:
    has_bs4 = False
    logger.warning("[JinaSum] BeautifulSoup库未安装，无法使用部分内容提取功能")

try:
    from requests_html import HTMLSession
    has_requests_html = True
except ImportError:
    has_requests_html = False
    logger.warning("[JinaSum] requests_html库未安装，动态内容提取功能将不可用")

# 总体判断是否可以使用高级内容提取方法
can_use_advanced_extraction = has_bs4 and has_requests

@plugins.register(
    name="JinaSum",
    desire_priority=20,
    hidden=False,
    desc="Sum url link content with jina reader and llm",
    version="1.1.0",
    author="sofs2005",
)
class JinaSum(Plugin):
    """网页内容总结插件

    功能：
    1. 自动总结分享的网页内容
    2. 支持手动触发总结
    3. 支持群聊和单聊不同处理方式
    4. 支持黑名单群组配置
    """

    # 默认配置
    DEFAULT_CONFIG = {
        # 基础配置
        "jina_reader_base": "https://r.jina.ai",
        "max_words": 8000,
        "prompt": "用简洁凝练的中文对以下文本内容进行总结，总结输出包括以下三个部分(除此之外无需任何额外的解释，总字数不超过300字)：\n📖 一句话总结\n 💡关键要点,用数字序号列出3-5个文章的核心内容\n🔖 标签: #xx #xx\n请使用emoji让你的表达更生动。",

        # OpenAI API 配置
        "open_ai_api_base": "",
        "open_ai_api_key": "",
        "open_ai_model": "gpt-3.5-turbo",

        # URL 白名单和黑名单
        "white_url_list": [],
        "black_url_list": [
            "https://support.weixin.qq.com",  # 视频号视频
            "https://channels-aladin.wxqcloud.qq.com",  # 视频号音乐
        ],

        # 用户和群组控制
        "auto_sum": True,
        "white_user_list": [],  # 私聊白名单
        "black_user_list": [],  # 私聊黑名单
        "white_group_list": [],  # 群聊白名单
        "black_group_list": [],  # 群聊黑名单

        # 缓存和超时设置
        "pending_messages_timeout": 60,  # 分享消息缓存时间（默认 60 秒）
        "content_cache_timeout": 300,  # 总结后提问的缓存时间（默认 5 分钟）

        # 触发词设置
        "qa_trigger": "问",  # 提问触发词
    }

    def __init__(self):
        super().__init__()
        try:
            # 加载配置
            self.config = self._load_config()
            if not self.config:
                raise Exception("配置加载失败")

            # 使用配置更新实例属性，找不到时使用默认值
            for key, default_value in self.DEFAULT_CONFIG.items():
                setattr(self, key, self.config.get(key, default_value))

            # 验证必需的配置
            if not self.open_ai_api_key:
                raise Exception("OpenAI API 密钥未配置")

            # 每次启动时重置缓存
            self.pending_messages = {}  # 待处理消息缓存
            self.content_cache = {}  # 按 chat_id 缓存总结内容

            logger.info(f"[JinaSum] inited, config={self.config}")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context

        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{e}")
            raise

    def _load_config(self):
        """从配置文件加载配置"""
        try:
            # 1. 使用父类方法按优先级加载插件配置（上级目录 > 插件目录 > 模板文件）
            config = super().load_config() or {}

            # 2. 加载主配置文件
            main_config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json")
            if os.path.exists(main_config_path):
                with open(main_config_path, "r", encoding="utf-8") as f:
                    main_config = json.load(f)
                    # 直接设置group_chat_prefix实例属性
                    self.group_chat_prefix = main_config.get('group_chat_prefix', ["小爱","@小爱"])
            else:
                logger.error("[JinaSum] 未找到主配置文件")
                raise Exception("主配置文件不存在")

            # 3. 使用默认配置补充缺失的值
            for key, default_value in self.DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = default_value

            return config

        except Exception as e:
            logger.error(f"[JinaSum] 加载配置失败: {e}")
            raise

    def _get_user_info_from_msg(self, msg, context):
        """从消息中获取用户信息"""
        try:
            # 获取用户备注名
            user_remark = None
            if hasattr(msg, '_rawmsg'):
                raw_msg = msg._rawmsg
                if isinstance(raw_msg, dict) and 'Data' in raw_msg:
                    msg_data = raw_msg['Data']
                    if 'PushContent' in msg_data:
                        push_content = msg_data['PushContent']
                        remark_match = re.match(r'^([^:：在]+)(?:\s*[:：]|\s*在群聊中)', push_content)
                        if remark_match:
                            user_remark = remark_match.group(1).strip()

            is_group = context.get("isgroup", False)

            if is_group:
                chat_id = msg.from_user_id  # 群ID
                user_id = msg.actual_user_id  # 发送者ID
                user_name = msg.actual_user_nickname  # 发送者昵称
                group_name = msg.other_user_nickname  # 群名称
                display_name = user_remark or group_name or chat_id  # 显示名称
                return {
                    'chat_id': chat_id,
                    'user_id': user_id,
                    'user_name': user_name,
                    'display_name': display_name,
                    'group_name': group_name,
                    'is_group': True
                }
            else:
                user_id = msg.from_user_id
                user_name = msg.actual_user_nickname or msg.from_user_nickname
                display_name = user_remark or user_name or user_id
                return {
                    'chat_id': user_id,
                    'user_id': user_id,
                    'user_name': user_name,
                    'display_name': display_name,
                    'group_name': None,
                    'is_group': False
                }
        except Exception as e:
            logger.error(f"[JinaSum] 获取用户信息失败: {e}")
            return None

    def _should_auto_summarize(self, user_info: dict) -> bool:
        """检查是否应该自动总结"""
        try:
            if not user_info:
                return self.auto_sum

            if user_info['is_group']:
                # 群聊权限检查
                group_identifiers = [
                    user_info['chat_id'],  # 群ID
                    user_info['group_name'],  # 群名称
                    user_info['display_name']  # 显示名称
                ]

                # 黑名单优先
                if any(identifier in self.black_group_list
                      for identifier in group_identifiers if identifier):
                    return False

                # 白名单次之
                if self.white_group_list:
                    if any(identifier in self.white_group_list
                          for identifier in group_identifiers if identifier):
                        return True
                    return False

                return self.auto_sum
            else:
                # 私聊权限检查
                user_identifiers = [
                    user_info['user_id'],  # 用户ID
                    user_info['user_name'],  # 用户昵称
                    user_info['display_name']  # 显示名称
                ]

                # 黑名单优先
                if any(identifier in self.black_user_list
                      for identifier in user_identifiers if identifier):
                    return False

                # 白名单次之
                if self.white_user_list:
                    if any(identifier in self.white_user_list
                          for identifier in user_identifiers if identifier):
                        return True
                    return False

                return self.auto_sum

        except Exception as e:
            logger.error(f"[JinaSum] 检查自动总结权限失败: {e}")
            return self.auto_sum

    def on_handle_context(self, e_context: EventContext):
        """处理消息"""
        context = e_context['context']
        if context.type not in [ContextType.TEXT, ContextType.SHARING]:
            return

        content = context.content
        msg = e_context["context"]["msg"]

        # 获取用户信息
        user_info = self._get_user_info_from_msg(msg, context)
        if not user_info:
            return

        # 检查是否需要自动总结
        should_auto_sum = self._should_auto_summarize(user_info)

        # 清理过期缓存
        self._clean_expired_cache()

        # 处理分享消息
        if context.type == ContextType.SHARING:
            logger.debug(f"[JinaSum] Processing SHARING message, chat_id: {user_info['chat_id']}")
            # 新增日志: 打印is_group状态和即将检查的content (URL)
            logger.info(f"[JinaSum] Pre-check_url: is_group={user_info['is_group']}, content_to_check='{content}'")
            # 检查 URL 是否有效
            if not self._check_url(content): # 这一行是 self._check_url(content) 的调用点
                reply = Reply(ReplyType.TEXT, "无效的URL或被禁止的URL。")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            if user_info['is_group']:
                if should_auto_sum:
                    return self._process_summary(content, e_context, user_info['chat_id'], retry_count=0)
                else:
                    self.pending_messages[user_info['chat_id']] = {
                        "content": content,
                        "timestamp": time.time(),
                    }
                    logger.debug(f"[JinaSum] Cached SHARING message: {content}, chat_id: {user_info['chat_id']}")
                    return
            else:  # 单聊消息
                if should_auto_sum:
                    return self._process_summary(content, e_context, user_info['chat_id'], retry_count=0)
                else:
                    logger.debug(f"[JinaSum] User {user_info['display_name']} not in whitelist, require '总结' to trigger summary")
                    return

        # 处理文本消息
        elif context.type == ContextType.TEXT:
            logger.debug(f"[JinaSum] Processing TEXT message, chat_id: {user_info['chat_id']}")
            content = content.strip()

            # 获取群聊前缀列表
            group_chat_prefix = self.group_chat_prefix

            # 处理群聊消息
            if user_info['is_group']:
                # 遍历前缀列表，检查消息内容是否以这些前缀开头
                for prefix in group_chat_prefix:
                    pattern = r'^\s*{}\s+'.format(re.escape(prefix))
                    if re.match(pattern, content):
                        # 去掉前缀和前后的空格
                        content = re.sub(pattern, '', content)
                        break

            # 解析总结指令
            custom_prompt, url = self._parse_command(content)
            if url or custom_prompt:  # 处理总结指令
                if url:  # 直接URL总结
                    return self._process_summary(url, e_context, user_info['chat_id'], custom_prompt=custom_prompt)
                elif user_info['chat_id'] in self.pending_messages:  # 处理缓存内容
                    cached_content = self.pending_messages[user_info['chat_id']]["content"]
                    del self.pending_messages[user_info['chat_id']]
                    return self._process_summary(cached_content, e_context, user_info['chat_id'], skip_notice=True, custom_prompt=custom_prompt)
                else:
                    logger.debug("[JinaSum] No content to summarize")
                    return

            # 添加处理问答触发词的逻辑
            if hasattr(self, "qa_trigger") and content.startswith(self.qa_trigger):
                # 去掉触发词和空格,获取实际问题
                question = content[len(self.qa_trigger):].strip()
                if question:  # 确保问题不为空
                    logger.debug(f"[JinaSum] Processing question: {question}")
                    return self._process_question(question, user_info['chat_id'], e_context)
                else:
                    logger.debug("[JinaSum] Empty question")
                    return

    def _clean_expired_cache(self):
        """清理过期的缓存"""
        current_time = time.time()
        # 清理待处理消息缓存
        expired_keys = [
            k
            for k, v in self.pending_messages.items()
            if current_time - v["timestamp"] > self.pending_messages_timeout
        ]
        for k in expired_keys:
            del self.pending_messages[k]

        # 清理 content_cache 中过期的数据
        expired_chat_ids = [
            k
            for k, v in self.content_cache.items()
            if current_time - v["timestamp"] > self.content_cache_timeout
        ]
        for k in expired_chat_ids:
            del self.content_cache[k]

    def _process_summary(self, content: str, e_context: EventContext, chat_id: str, retry_count: int = 0, skip_notice: bool = False, custom_prompt: str = None):
        """处理总结请求

        Args:
            content: 要处理的内容
            e_context: 事件上下文
            chat_id: 群名称或用户昵称
            retry_count: 重试次数
            skip_notice: 是否跳过提示消息
        """
        try:
            if retry_count == 0 and not skip_notice:
                logger.debug(f"[JinaSum] Processing URL: {content}, chat_id: {chat_id}")
                reply = Reply(ReplyType.TEXT, "🎉正在为您生成总结，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            # 获取网页内容
            target_url = html.unescape(content)
            jina_url = self._get_jina_url(target_url)
            logger.debug(f"[JinaSum] Requesting jina url: {jina_url}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
            try:
                response = requests.get(jina_url, headers=headers, timeout=60)
                response.raise_for_status()
                target_url_content = response.text

                # 检查是否是微信平台文章，并检查返回内容是否包含"环境异常"
                if "mp.weixin.qq.com" in target_url:
                    if not target_url_content or "环境异常" in target_url_content:
                        logger.error(f"[JinaSum] 微信平台文章内容获取失败或包含'环境异常': {target_url}")
                        # 尝试使用备用方法获取内容
                        if can_use_advanced_extraction:
                            logger.info(f"[JinaSum] 尝试使用通用内容提取方法获取微信文章: {target_url}")
                            extracted_content = self._extract_content_general(target_url)
                            if extracted_content and len(extracted_content) > 500 and "环境异常" not in extracted_content:
                                logger.info(f"[JinaSum] 通用内容提取方法成功获取微信文章: {target_url}, 内容长度: {len(extracted_content)}")
                                target_url_content = extracted_content
                            elif has_requests_html:
                                logger.info(f"[JinaSum] 尝试使用动态内容提取方法获取微信文章: {target_url}")
                                dynamic_content = self._extract_dynamic_content(target_url)
                                if dynamic_content and len(dynamic_content) > 500 and "环境异常" not in dynamic_content:
                                    logger.info(f"[JinaSum] 动态内容提取方法成功获取微信文章: {target_url}, 内容长度: {len(dynamic_content)}")
                                    target_url_content = dynamic_content
                                else:
                                    if not dynamic_content or len(dynamic_content) <= 500:
                                        logger.warning(f"[JinaSum] 动态内容提取方法获取的微信文章内容过短或为空: {target_url}")
                                    elif "环境异常" in dynamic_content:
                                        logger.warning(f"[JinaSum] 动态内容提取方法获取的微信文章内容包含'环境异常': {target_url}")
                                    raise ValueError("无法获取微信平台文章内容")
                            else:
                                raise ValueError("无法获取微信平台文章内容")
                        else:
                            raise ValueError("无法获取微信平台文章内容，且未安装高级内容提取所需的库")
                else:
                    # 非微信平台文章，只检查内容是否为空
                    if not target_url_content:
                        logger.error(f"[JinaSum] 内容获取失败，返回为空: {target_url}")
                        # 尝试使用备用方法获取内容
                        if can_use_advanced_extraction:
                            logger.info(f"[JinaSum] 尝试使用通用内容提取方法: {target_url}")
                            extracted_content = self._extract_content_general(target_url)
                            if extracted_content and len(extracted_content) > 500:
                                logger.info(f"[JinaSum] 通用内容提取方法成功: {target_url}, 内容长度: {len(extracted_content)}")
                                target_url_content = extracted_content
                            elif has_requests_html:
                                logger.info(f"[JinaSum] 尝试使用动态内容提取方法: {target_url}")
                                dynamic_content = self._extract_dynamic_content(target_url)
                                if dynamic_content and len(dynamic_content) > 500:
                                    logger.info(f"[JinaSum] 动态内容提取方法成功: {target_url}, 内容长度: {len(dynamic_content)}")
                                    target_url_content = dynamic_content
                                else:
                                    logger.warning(f"[JinaSum] 动态内容提取方法获取的内容过短或为空: {target_url}")
                                    raise ValueError("Empty response from all content extraction methods")
                            else:
                                raise ValueError("Empty response from jina reader and no advanced extraction methods available")
                        else:
                            raise ValueError("Empty response from jina reader")
            except Exception as e:
                logger.error(f"[JinaSum] Failed to get content from jina reader: {str(e)}")
                if retry_count < 3:
                    logger.info(f"[JinaSum] Jina Reader Retrying {retry_count + 1}/3...")
                    time.sleep(1) # Jina Reader 异常时重试间隔 1 秒
                    return self._process_summary(content, e_context, chat_id, retry_count + 1)

                reply = Reply(ReplyType.ERROR, f"无法获取该内容: {str(e)}")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            try:
                # 使用统一的内容处理方法
                summary = self._process_content_query(target_url_content, custom_prompt, e_context)
                additional_prompt = "\n\n💬5min内输入j追问+问题，可继续追问"
                summary += additional_prompt
                reply = Reply(ReplyType.TEXT, summary)
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

                # 缓存内容和时间戳，按 chat_id 缓存
                self.content_cache[chat_id] = {
                    "url": target_url,
                    "content": target_url_content,
                    "timestamp": time.time(),
                }
                logger.debug(f"[JinaSum] Content cached for chat_id: {chat_id}")

            except Exception as e:
                logger.error(f"[JinaSum] Failed to get summary from OpenAI: {str(e)}")
                if retry_count < 3:
                    logger.info(f"[JinaSum] OpenAI API Retrying {retry_count + 1}/3...")
                    time.sleep(1) # OpenAI API 异常时重试间隔 2 秒
                    return self._process_summary(content, e_context, chat_id, retry_count + 1)
                reply = Reply(ReplyType.ERROR, f"内容总结出现错误: {str(e)}")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing summary: {str(e)}", exc_info=True)
            if retry_count < 3:
                logger.info(f"[JinaSum] Retrying {retry_count + 1}/3...")
                time.sleep(1) # 其他异常也增加1秒间隔
                return self._process_summary(content, e_context, chat_id, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"无法获取该内容: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def _process_question(self, question: str, chat_id: str, e_context: EventContext, retry_count: int = 0):
        """处理问题"""
        try:
            # 使用 chat_id (群名称或用户昵称) 作为键从 content_cache 中获取缓存内容
            cache_data = self.content_cache.get(chat_id)
            if (cache_data and time.time() - cache_data["timestamp"] <= self.content_cache_timeout):
                recent_content = cache_data["content"]
            else:
                logger.debug(f"[JinaSum] No valid content cache found or content expired for chat_id: {chat_id}")
                reply = Reply(ReplyType.TEXT, "总结内容已过期或不存在，请重新总结后重试。")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            if retry_count == 0:
                reply = Reply(ReplyType.TEXT, "🤔 正在思考您的问题，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            try:
                # 使用统一的内容处理方法
                answer = self._process_content_query(recent_content, question, e_context)
                reply = Reply(ReplyType.TEXT, answer)
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

            except Exception as e:
                logger.error(f"[JinaSum] Failed to get answer from OpenAI: {str(e)}")
                if retry_count < 3:
                    logger.info(f"[JinaSum] OpenAI API Retrying {retry_count + 1}/3...")
                    time.sleep(1)
                    return self._process_question(question, chat_id, e_context, retry_count + 1)
                reply = Reply(ReplyType.ERROR, f"处理问题时出现错误: {str(e)}")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing question: {str(e)}")
            if retry_count < 3:
                logger.info(f"[JinaSum] Retrying {retry_count + 1}/3...")
                time.sleep(1) # 其他异常也增加1秒间隔
                return self._process_question(question, chat_id, e_context, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"处理问题时出现错误: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose=False, **kwargs):
        help_text = "网页内容总结\n"
        if not verbose:
            return help_text

        help_text += "使用方法:\n"
        help_text += "1. 总结网页内容:\n"
        help_text += "   - 总结 网址 (总结指定网页的内容)\n"

        if self.auto_sum:
            help_text += "2. 单聊时，默认自动总结分享消息或URL\n"
            if self.black_user_list:
                help_text += "   (黑名单用户需要发送「总结」才能触发)\n"
            if self.white_user_list:
                help_text += "   (白名单用户将自动总结)\n"
            help_text += "3. 群聊中，默认自动总结分享消息或URL\n"
            if self.black_group_list:
                help_text += "   (黑名单群组需要发送「总结」才能触发)\n"
            if self.white_group_list:
                help_text += "   (白名单群组将自动总结)\n"
        else:
            help_text += "2. 单聊时，需要发送「总结」才能触发总结， 白名单用户除外。\n"
            if self.white_user_list:
                help_text += "  (白名单用户将自动总结)\n"
            help_text += "3. 群聊中，需要发送「总结」才能触发总结，白名单群组除外。\n"
            if self.white_group_list:
                 help_text += "  (白名单群组将自动总结)\n"

        if hasattr(self, "qa_trigger"):
            help_text += (
                f"4. 总结完成后{self.content_cache_timeout//60}分钟内，可以发送「{self.qa_trigger}xxx」来询问文章相关问题\n"
            )

        help_text += f"注：手动触发的网页总结指令需要在{self.pending_messages_timeout}秒内发出"
        return help_text

    def _get_jina_url(self, target_url):
        # 只对微信公众号链接做特殊处理
        if "mp.weixin.qq.com" in target_url:
            # 使用完全编码的方式处理微信URL，safe=''确保所有字符都被编码
            encoded_url = quote(target_url, safe='')
            logger.info(f"[JinaSum] 微信平台文章，使用完全编码: {encoded_url}")
            return self.jina_reader_base + "/" + encoded_url
        else:
            # 其他网站保持原有处理方式
            logger.info(f"[JinaSum] 非微信平台文章，使用原始URL")
            return self.jina_reader_base + "/" + target_url

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"

    def _get_openai_headers(self):
        return {
            "Authorization": f"Bearer {self.open_ai_api_key}",
            "Host": urlparse(self.open_ai_api_base).netloc,
            "Content-Type": "application/json",
        }

    def _get_openai_payload(self, target_url_content):
        target_url_content = target_url_content[: self.max_words]
        sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
        messages = [{"role": "user", "content": sum_prompt}]
        payload = {
            "model": self.open_ai_model,
            "messages": messages,
        }
        return payload

    def _check_url(self, target_url: str):
        """检查URL是否有效且允许访问

        Args:
            target_url: 要检查的URL

        Returns:
            bool: URL是否有效且允许访问
        """
        stripped_url = target_url.strip()
        parsed_url = urlparse(stripped_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            return False

        # 检查黑名单，黑名单优先
        for black_url in self.black_url_list:
            if stripped_url.startswith(black_url):
                return False

        # 如果有白名单，则检查是否在白名单中
        if self.white_url_list:
            if not any(stripped_url.startswith(white_url) for white_url in self.white_url_list):
                return False

        return True

    def _parse_command(self, content: str):
        """解析总结指令
        返回: (custom_prompt, url)

        支持的格式:
        1. 总结
        2. 总结 [URL]
        3. 总结 自定义问题
        4. 总结 自定义问题 [URL]
        """
        # 移除多余空格，但保留单词间的空格
        content = ' '.join(content.split())

        # 检查是否以"总结"开头
        if not content.startswith("总结"):
            return None, None

        # 去掉开头的"总结"和空格
        content = content[2:].strip()
        if not content:  # 只有"总结"
            return None, None

        # 检查最后一部分是否是URL
        parts = content.split()
        if self._check_url(parts[-1]):  # 最后一部分是URL
            url = parts[-1]
            custom_prompt = " ".join(parts[:-1]).strip()  # URL前的所有内容作为提示词
        else:  # 没有URL
            url = None
            custom_prompt = content.strip()

        # 如果custom_prompt为空，说明是普通的URL总结
        if not custom_prompt:
            return None, url

        return custom_prompt, url

    def _get_default_headers(self):
        """获取默认请求头"""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
        ]
        selected_ua = random.choice(user_agents)

        return {
            "User-Agent": selected_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        }

    def _extract_content_general(self, url, headers=None):
        """通用网页内容提取方法，支持静态和动态页面

        首先尝试静态提取（更快、更轻量），如果失败或内容太少再尝试动态提取（更慢但更强大）

        Args:
            url: 网页URL
            headers: 可选的请求头，如果为None则使用默认

        Returns:
            str: 提取的内容，失败返回None
        """
        if not has_bs4:
            logger.error("[JinaSum] BeautifulSoup库未安装，无法使用通用内容提取方法")
            return None

        try:
            # 如果没有提供headers，创建一个默认的
            if not headers:
                headers = self._get_default_headers()

            # 添加随机延迟以避免被检测为爬虫
            time.sleep(random.uniform(0.5, 2))

            # 创建会话对象
            session = requests.Session()

            # 设置基本cookies
            session.cookies.update({
                f"visit_id_{int(time.time())}": f"{random.randint(1000000, 9999999)}",
                "has_visited": "1",
            })

            # 发送请求获取页面
            logger.debug(f"[JinaSum] 通用提取方法正在请求: {url}")
            response = session.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            # 确保编码正确
            if response.encoding == 'ISO-8859-1':
                response.encoding = response.apparent_encoding

            # 使用BeautifulSoup解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')

            # 移除无用元素
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form', 'iframe']):
                element.extract()

            # 寻找可能的标题
            title = None

            # 尝试多种标题选择器
            title_candidates = [
                soup.select_one('h1'),  # 最常见的标题标签
                soup.select_one('title'),  # HTML标题
                soup.select_one('.title'),  # 常见的标题类
                soup.select_one('.article-title'),  # 常见的文章标题类
                soup.select_one('.post-title'),  # 博客标题
                soup.select_one('[class*="title" i]'),  # 包含title的类
            ]

            for candidate in title_candidates:
                if candidate and candidate.text.strip():
                    title = candidate.text.strip()
                    break

            # 查找可能的内容元素
            content_candidates = []

            # 1. 尝试找常见的内容容器
            content_selectors = [
                'article', 'main', '.content', '.article', '.post-content',
                '[class*="content" i]', '[class*="article" i]',
                '.story', '.entry-content', '.post-body',
                '#content', '#article', '.body'
            ]

            for selector in content_selectors:
                elements = soup.select(selector)
                if elements:
                    content_candidates.extend(elements)

            # 2. 如果没有找到明确的内容容器，寻找具有最多文本的div元素
            if not content_candidates:
                paragraphs = {}
                # 查找所有段落和div
                for elem in soup.find_all(['p', 'div']):
                    text = elem.get_text(strip=True)
                    # 只考虑有实际内容的元素
                    if len(text) > 100:
                        paragraphs[elem] = len(text)

                # 找出文本最多的元素
                if paragraphs:
                    max_elem = max(paragraphs.items(), key=lambda x: x[1])[0]
                    # 如果是div，直接添加；如果是p，尝试找其父元素
                    if max_elem.name == 'div':
                        content_candidates.append(max_elem)
                    else:
                        # 找包含多个段落的父元素
                        parent = max_elem.parent
                        if parent and len(parent.find_all('p')) > 3:
                            content_candidates.append(parent)
                        else:
                            content_candidates.append(max_elem)

            # 3. 简单算法来评分和选择最佳内容元素
            best_content = None
            max_score = 0

            for element in content_candidates:
                # 计算文本长度
                text = element.get_text(strip=True)
                text_length = len(text)

                # 计算文本密度（文本长度/HTML长度）
                html_length = len(str(element))
                text_density = text_length / html_length if html_length > 0 else 0

                # 计算段落数量
                paragraphs = element.find_all('p')
                paragraph_count = len(paragraphs)

                # 检查是否有图片
                images = element.find_all('img')
                image_count = len(images)

                # 根据各种特征计算分数
                score = (
                    text_length * 1.0 +  # 文本长度很重要
                    text_density * 100 +  # 文本密度很重要
                    paragraph_count * 30 +  # 段落数量也很重要
                    image_count * 10  # 图片不太重要，但也是一个指标
                )

                # 减分项：如果包含许多链接，可能是导航或侧边栏
                links = element.find_all('a')
                link_text_ratio = sum(len(a.get_text(strip=True)) for a in links) / text_length if text_length > 0 else 0
                if link_text_ratio > 0.5:  # 如果链接文本占比过高
                    score *= 0.5

                # 更新最佳内容
                if score > max_score:
                    max_score = score
                    best_content = element

            # 如果找到内容，提取并清理文本
            static_content_result = None
            if best_content:
                # 首先移除内容中可能的广告或无关元素
                for ad in best_content.select('[class*="ad" i], [class*="banner" i], [id*="ad" i], [class*="recommend" i]'):
                    ad.extract()

                # 获取并清理文本
                content_text = best_content.get_text(separator='\n', strip=True)

                # 移除多余的空白行
                content_text = re.sub(r'\n{3,}', '\n\n', content_text)

                # 构建最终输出
                result = ""
                if title:
                    result += f"标题: {title}\n\n"

                result += content_text

                logger.debug(f"[JinaSum] 通用提取方法成功，提取内容长度: {len(result)}")
                static_content_result = result

            # 判断静态提取的内容质量
            content_is_good = False
            if static_content_result:
                # 内容长度检查
                if len(static_content_result) > 1000:
                    content_is_good = True
                # 结构检查 - 至少应该有多个段落
                elif static_content_result.count('\n\n') >= 3:
                    content_is_good = True

            # 如果静态提取内容质量不佳，尝试动态提取
            if not content_is_good:
                logger.debug("[JinaSum] 静态提取内容质量不佳，尝试动态提取")
                dynamic_content = self._extract_dynamic_content(url, headers)
                if dynamic_content:
                    logger.debug(f"[JinaSum] 动态提取成功，内容长度: {len(dynamic_content)}")
                    return dynamic_content

            return static_content_result

        except Exception as e:
            logger.error(f"[JinaSum] 通用内容提取方法失败: {str(e)}")
            return None

    def _extract_dynamic_content(self, url, headers=None):
        """使用JavaScript渲染提取动态页面内容

        Args:
            url: 网页URL
            headers: 可选的请求头

        Returns:
            str: 提取的内容，失败返回None
        """
        if not has_requests_html:
            logger.error("[JinaSum] requests_html库未安装，无法使用动态内容提取方法")
            return None

        try:
            logger.debug(f"[JinaSum] 开始动态提取内容: {url}")

            # 创建会话并设置超时
            session = HTMLSession()

            # 添加请求头
            req_headers = headers or self._get_default_headers()

            # 获取页面
            response = session.get(url, headers=req_headers, timeout=30)

            # 执行JavaScript (设置超时，防止无限等待)
            logger.debug("[JinaSum] 开始执行JavaScript")
            response.html.render(timeout=20, sleep=2)
            logger.debug("[JinaSum] JavaScript执行完成")

            # 处理渲染后的HTML
            rendered_html = response.html.html

            # 使用BeautifulSoup解析渲染后的HTML
            soup = BeautifulSoup(rendered_html, 'html.parser')

            # 清理无用元素
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                element.extract()

            # 查找标题
            title = None
            title_candidates = [
                soup.select_one('h1'),
                soup.select_one('title'),
                soup.select_one('.title'),
                soup.select_one('[class*="title" i]'),
            ]

            for candidate in title_candidates:
                if candidate and candidate.text.strip():
                    title = candidate.text.strip()
                    break

            # 寻找主要内容
            main_content = None

            # 1. 尝试找主要内容容器
            main_selectors = [
                'article', 'main', '.content', '.article',
                '[class*="content" i]', '[class*="article" i]',
                '#content', '#article'
            ]

            for selector in main_selectors:
                elements = soup.select(selector)
                if elements:
                    # 选择包含最多文本的元素
                    main_content = max(elements, key=lambda x: len(x.get_text()))
                    break

            # 2. 如果没找到，寻找文本最多的div
            if not main_content:
                paragraphs = {}
                for elem in soup.find_all(['div']):
                    text = elem.get_text(strip=True)
                    if len(text) > 200:  # 只考虑长文本
                        paragraphs[elem] = len(text)

                if paragraphs:
                    main_content = max(paragraphs.items(), key=lambda x: x[1])[0]

            # 3. 如果还是没找到，使用整个body
            if not main_content:
                main_content = soup.body

            # 从主要内容中提取文本
            if main_content:
                # 清理可能的广告或无关元素
                for ad in main_content.select('[class*="ad" i], [class*="banner" i], [id*="ad" i], [class*="recommend" i]'):
                    ad.extract()

                # 获取文本
                content_text = main_content.get_text(separator='\n', strip=True)
                content_text = re.sub(r'\n{3,}', '\n\n', content_text)  # 清理多余空行

                # 构建最终结果
                result = ""
                if title:
                    result += f"标题: {title}\n\n"
                result += content_text

                # 关闭会话
                session.close()

                return result

            # 关闭会话
            session.close()

            return None

        except Exception as e:
            logger.error(f"[JinaSum] 动态提取失败: {str(e)}")
            return None

    def _process_content_query(self, content: str, query: str, e_context: EventContext):
        """统一处理内容查询
        Args:
            content: 文章内容
            query: 用户查询(可以是总结请求或问题)
            e_context: 事件上下文
        """
        try:
            # 限制内容长度
            content = content[:self.max_words]

            # 构建prompt
            if query:
                # 修改这里,让自定义总结和问答使用相同的提问方式
                prompt = f"请根据以下引号内的文章内容回答以下问题：{query}\n\n'''{content}'''"
            else:
                # 使用默认总结模板
                prompt = f"{self.prompt}\n\n'''{content}'''"

            # 准备API请求
            openai_payload = {
                "model": self.open_ai_model,
                "messages": [{"role": "user", "content": prompt}],
            }

            # 调用API
            openai_chat_url = self._get_openai_chat_url()
            openai_headers = self._get_openai_headers()
            response = requests.post(
                openai_chat_url, headers=openai_headers, json=openai_payload, timeout=60
            )
            response.raise_for_status()

            # 获取回答
            answer = response.json()["choices"][0]["message"]["content"]
            return answer

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing content query: {str(e)}")
            raise
