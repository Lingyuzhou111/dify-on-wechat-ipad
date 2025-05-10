import os
import time
import json
import xml.etree.ElementTree as ET
from typing import Dict, Any

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from config import conf

class WX849Message(ChatMessage):
    """
    wx849 消息处理类 - 简化版，无日志输出
    """
    def __init__(self, msg: Dict[str, Any], is_group: bool = False):
        super().__init__(msg)
        self.msg = msg
        
        # 提取消息基本信息
        self.msg_id = msg.get("msgid", msg.get("MsgId", msg.get("id", "")))
        if not self.msg_id:
            self.msg_id = f"msg_{int(time.time())}_{hash(str(msg))}"
        
        self.create_time = msg.get("timestamp", msg.get("CreateTime", msg.get("createTime", int(time.time()))))
        self.is_group = is_group
        
        # 提取发送者和接收者ID
        self.from_user_id = self._get_string_value(msg.get("fromUserName", msg.get("FromUserName", "")))
        self.to_user_id = self._get_string_value(msg.get("toUserName", msg.get("ToUserName", "")))
        
        # 提取消息内容
        self.content = self._get_string_value(msg.get("content", msg.get("Content", "")))
        
        # 获取消息类型
        self.msg_type = msg.get("type", msg.get("Type", msg.get("MsgType", 0)))
        
        # 初始化其他字段
        self.sender_wxid = ""      # 实际发送者ID
        self.at_list = []          # 被@的用户列表
        self.ctype = ContextType.UNKNOWN
        self.self_display_name = "" # 机器人在群内的昵称
        
        # 添加actual_user_id和actual_user_nickname字段，与sender_wxid保持一致
        self.actual_user_id = ""    # 实际发送者ID
        self.actual_user_nickname = "" # 实际发送者昵称
        
        self._convert_msg_type_to_ctype()
        self.type = self.ctype  # Ensure self.type attribute exists and holds the ContextType value
        
        # 尝试从MsgSource中提取机器人在群内的昵称
        try:
            msg_source = msg.get("MsgSource", "")
            if msg_source and ("<msgsource>" in msg_source.lower() or msg_source.startswith("<")):
                root = ET.fromstring(msg_source if "<msgsource>" in msg_source.lower() else f"<msgsource>{msg_source}</msgsource>")
                
                # 查找displayname或其他可能包含群昵称的字段
                for tag in ["selfDisplayName", "displayname", "nickname"]:
                    elem = root.find(f".//{tag}")
                    if elem is not None and elem.text:
                        self.self_display_name = elem.text
                        break
        except Exception as e:
            # 解析失败，保持为空字符串
            pass
    
    def _convert_msg_type_to_ctype(self):
        """
        Converts the raw message type (self.msg_type) to ContextType (self.ctype).
        WX849/iPad协议原始消息类型:
        1: 文本
        3: 图片
        34: 语音
        37: 好友确认 (添加好友请求)
        40: POSSIBLE_FRIEND_MSG (新的好友推荐)
        42: 名片
        43: 视频
        47: 表情/贴纸
        48: 位置
        49: App消息 (分享链接, 文件, 转账, 红包, 小程序等)
        51: 微信状态同步/操作通知
        62: 小视频
        10000: 系统消息
        10002: 撤回消息的系统提示
        """
        raw_type = str(self.msg_type) # Ensure it's a string

        if raw_type == "1":
            self.ctype = ContextType.TEXT
        elif raw_type == "3":
            self.ctype = ContextType.IMAGE
        elif raw_type == "34":
            self.ctype = ContextType.VOICE
        elif raw_type == "43" or raw_type == "62": # Video and Short Video
            self.ctype = ContextType.VIDEO
        elif raw_type == "47": # Sticker/Emoji
            self.ctype = ContextType.EMOJI 
        elif raw_type == "49": # App Message (XML based)
            self.ctype = ContextType.XML
        elif raw_type == "42": # Contact Card
            self.ctype = ContextType.XML # Or a specific ContextType.CONTACT_CARD
        elif raw_type == "48": # Location
            self.ctype = ContextType.XML # Or a specific ContextType.LOCATION
        elif raw_type == "37" or raw_type == "40": # Friend request, recommendation
            self.ctype = ContextType.SYSTEM # Or ContextType.INFO
        elif raw_type == "51": # Status/Operation (e.g. friend verified, typing)
            # Assuming ContextType.STATUS_SYNC exists or is mapped to SYSTEM/INFO
            if hasattr(ContextType, 'STATUS_SYNC'):
                self.ctype = ContextType.STATUS_SYNC
            else:
                self.ctype = ContextType.SYSTEM # Fallback if STATUS_SYNC not defined
        elif raw_type == "10000": # System message
            self.ctype = ContextType.INFO # Or ContextType.SYSTEM
        elif raw_type == "10002": # System message for recalled message
            # Assuming ContextType.RECALLED exists or is mapped to SYSTEM/INFO
            if hasattr(ContextType, 'RECALLED'):
                self.ctype = ContextType.RECALLED
            else:
                self.ctype = ContextType.SYSTEM # Fallback if RECALLED not defined
        else:
            # self.ctype remains ContextType.UNKNOWN (as initialized)
            # Consider logging: logger.debug(f"[WX849Message] Unmapped raw msg_type: {self.msg_type}, ctype remains UNKNOWN.")
            pass
    
    def _get_string_value(self, value):
        """确保值为字符串类型"""
        if isinstance(value, dict):
            return value.get("string", "")
        return str(value) if value is not None else ""
    
    # 以下是公开接口方法，提供给外部使用
    def get_content(self):
        """获取消息内容"""
        return self.content
    
    def get_type(self):
        """获取消息类型"""
        return self.ctype
    
    def get_msg_id(self):
        """获取消息ID"""
        return self.msg_id
    
    def get_create_time(self):
        """获取消息创建时间"""
        return self.create_time
    
    def get_from_user_id(self):
        """获取原始发送者ID"""
        return self.from_user_id
    
    def get_sender_id(self):
        """获取处理后的实际发送者ID（群聊中特别有用）"""
        return self.sender_wxid or self.from_user_id
    
    def get_to_user_id(self):
        """获取接收者ID"""
        return self.to_user_id
    
    def get_at_list(self):
        """获取被@的用户列表"""
        return self.at_list
    
    def is_at(self, wxid):
        """检查指定用户是否被@"""
        return wxid in self.at_list
    
    def is_group_message(self):
        """判断是否为群消息"""
        return self.is_group 
