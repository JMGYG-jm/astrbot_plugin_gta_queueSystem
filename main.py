import datetime
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Any

from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
import astrbot.api.message_components as Comp

logger = logging.getLogger("astrbot_plugin_task_queue")

@register("task_queue_plugin", "mogudunxy", "任务匹配系统", "1.0.0")
class TaskQueuePlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.config = config or {}
        
        # 数据结构 - 按群ID存储
        self.waiting_queues = {}      # {group_id: [user_id1, user_id2, ...]}
        self.pending_tasks_queues = {} # {group_id: [{"desc": "任务描述", "publisher": "发布者ID", "time": timestamp}, ...]}
        self.active_tasks = {}         # {group_id: {user_id: {"desc": task_desc, "start_time": timestamp}}}
        
        # 配置参数
        self.auto_clear_enabled = self.config.get("auto_clear_enabled", False)
        self.clear_time = self.config.get("clear_time", "23:59")
        self.queue_timeout = self.config.get("queue_timeout", 1200)
        self.task_timeout = self.config.get("task_timeout", 1200)
        
        logger.info("多群独立任务匹配系统初始化完成")
        
        # 启动定时清除任务
        if self.auto_clear_enabled:
            asyncio.create_task(self._daily_clear())
        
        # 启动超时检查任务
        asyncio.create_task(self._timeout_checker())
    
    def _is_task_message(self, message: str) -> bool:
        """
        判断是否是任务消息
        格式要求：任务名=数字 或 任务名等数字
        例如：副本=1、带刷深渊-2、帮忙=3
        """
        # 使用正则表达式匹配：任意文字 + [=-] + 数字(1-5)
        pattern = r'.+[=\等][1-5]$'
        return bool(re.match(pattern, message.strip()))
    
    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户ID"""
        if hasattr(event, 'user_id'):
            return str(event.user_id)
        elif hasattr(event, 'get_sender_id'):
            return str(event.get_sender_id())
        else:
            return "unknown"
    
    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """获取群组ID"""
        if event.is_private_chat():
            return None  # 私聊不支持
        
        if hasattr(event, 'group_id'):
            return str(event.group_id)
        elif hasattr(event, 'get_group_id'):
            return str(event.get_group_id())
        else:
            return None
    
    async def _daily_clear(self):
        """每日定时静默清除所有群的所有队列"""
        while True:
            try:
                now = datetime.datetime.now()
                target_time = datetime.datetime.strptime(self.clear_time, "%H:%M").time()
                target_datetime = datetime.datetime.combine(now.date(), target_time)
                
                if now > target_datetime:
                    target_datetime += datetime.timedelta(days=1)
                
                wait_seconds = (target_datetime - now).total_seconds()
                logger.info(f"距离下次定时清除还有 {wait_seconds/3600:.1f} 小时")
                
                await asyncio.sleep(wait_seconds)
                
                # 执行清除（所有群）
                total_count = 0
                for group_id in list(self.waiting_queues.keys()):
                    total_count += len(self.waiting_queues[group_id])
                for group_id in list(self.pending_tasks_queues.keys()):
                    total_count += len(self.pending_tasks_queues[group_id])
                for group_id in list(self.active_tasks.keys()):
                    total_count += len(self.active_tasks[group_id])
                
                self.waiting_queues.clear()
                self.pending_tasks_queues.clear()
                self.active_tasks.clear()
                
                logger.info(f"定时清除执行完毕，移除了 {total_count} 个队列项")
                
            except Exception as e:
                logger.error(f"定时清除任务出错: {e}")
                await asyncio.sleep(60)
    
    async def _timeout_checker(self):
        """定期检查超时的队列成员和任务（按群）"""
        while True:
            try:
                await asyncio.sleep(60)
                
                now = datetime.datetime.now().timestamp()
                total_removed = 0
                
                # 检查每个群的待匹配任务超时
                for group_id, tasks in list(self.pending_tasks_queues.items()):
                    before_count = len(tasks)
                    self.pending_tasks_queues[group_id] = [
                        t for t in tasks 
                        if now - datetime.datetime.fromisoformat(t["time"]).timestamp() < self.queue_timeout
                    ]
                    total_removed += before_count - len(self.pending_tasks_queues[group_id])
                    
                    # 如果群的任务列表为空，删除这个群的记录
                    if not self.pending_tasks_queues[group_id]:
                        del self.pending_tasks_queues[group_id]
                
                # 检查每个群的进行中任务超时
                for group_id, tasks in list(self.active_tasks.items()):
                    expired_tasks = []
                    for user_id, task_info in tasks.items():
                        if now - task_info["start_time"] > self.task_timeout:
                            expired_tasks.append(user_id)
                    
                    for user_id in expired_tasks:
                        del self.active_tasks[group_id][user_id]
                        total_removed += 1
                    
                    # 如果群的任务列表为空，删除这个群的记录
                    if not self.active_tasks[group_id]:
                        del self.active_tasks[group_id]
                
                if total_removed > 0:
                    logger.info(f"超时清理: 静默移除了 {total_removed} 个过期项")
                    
            except Exception as e:
                logger.error(f"超时检查出错: {e}")
                await asyncio.sleep(60)
    
    @filter.command_group("task")
    def task(self):
        pass
    
    @task.command("status")
    async def task_status(self, event: AstrMessageEvent):
        """查看状态"""
        if event.is_private_chat():
            yield event.plain_result("此功能仅在群聊中可用")
            return
        
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)
        
        # 检查是否在待命队列
        waiting_queue = self.waiting_queues.get(group_id, [])
        if user_id in waiting_queue:
            position = waiting_queue.index(user_id) + 1
            pending_tasks = self.pending_tasks_queues.get(group_id, [])
            yield event.plain_result(
                f"📋 您在待命队列第 {position} 位，共 {len(waiting_queue)} 人待命\n"
                f"📢 本群待匹配任务数：{len(pending_tasks)}"
            )
            return
        
        # 检查是否有发布的任务在等待
        pending_tasks = self.pending_tasks_queues.get(group_id, [])
        for task in pending_tasks:
            if task["publisher"] == user_id:
                wait_time = datetime.datetime.now() - datetime.datetime.fromisoformat(task["time"])
                minutes = int(wait_time.total_seconds() / 60)
                yield event.plain_result(
                    f"📢 您发布的任务正在等待匹配：{task['desc']}\n"
                    f"⏰ 已等待 {minutes} 分钟\n"
                    f"📋 本群当前待命人数：{len(waiting_queue)}"
                )
                return
        
        # 检查是否有已确认的任务
        group_active = self.active_tasks.get(group_id, {})
        if user_id in group_active:
            task_info = group_active[user_id]
            elapsed = datetime.datetime.now().timestamp() - task_info["start_time"]
            minutes = int(elapsed / 60)
            yield event.plain_result(
                f"✅ 您当前有进行中的任务：{task_info['desc']}\n"
                f"⏰ 已进行 {minutes} 分钟"
            )
            return
        
        yield event.plain_result("❌ 您不在任何队列中")
    
    @task.command("leave")
    async def task_leave(self, event: AstrMessageEvent):
        """退出队列"""
        if event.is_private_chat():
            yield event.plain_result("此功能仅在群聊中可用")
            return
        
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)
        left = False
        
        # 从待命队列移除
        if group_id in self.waiting_queues and user_id in self.waiting_queues[group_id]:
            self.waiting_queues[group_id].remove(user_id)
            if not self.waiting_queues[group_id]:
                del self.waiting_queues[group_id]
            left = True
        
        # 从待匹配任务移除
        if group_id in self.pending_tasks_queues:
            before_count = len(self.pending_tasks_queues[group_id])
            self.pending_tasks_queues[group_id] = [
                t for t in self.pending_tasks_queues[group_id] if t["publisher"] != user_id
            ]
            if len(self.pending_tasks_queues[group_id]) < before_count:
                left = True
            if not self.pending_tasks_queues[group_id]:
                del self.pending_tasks_queues[group_id]
        
        # 从进行中任务移除
        if group_id in self.active_tasks and user_id in self.active_tasks[group_id]:
            del self.active_tasks[group_id][user_id]
            if not self.active_tasks[group_id]:
                del self.active_tasks[group_id]
            left = True
        
        if left:
            yield event.plain_result("✅ 您已退出本群所有队列")
        else:
            yield event.plain_result("❌ 您不在本群任何队列中")
    
    @task.command("clear")
    async def task_clear(self, event: AstrMessageEvent):
        """清空当前群的所有队列（管理员）"""
        if event.is_private_chat():
            yield event.plain_result("此功能仅在群聊中可用")
            return
        
        group_id = self._get_group_id(event)
        
        before_count = 0
        if group_id in self.waiting_queues:
            before_count += len(self.waiting_queues[group_id])
            del self.waiting_queues[group_id]
        if group_id in self.pending_tasks_queues:
            before_count += len(self.pending_tasks_queues[group_id])
            del self.pending_tasks_queues[group_id]
        if group_id in self.active_tasks:
            before_count += len(self.active_tasks[group_id])
            del self.active_tasks[group_id]
        
        yield event.plain_result(f"✅ 已清空本群所有队列（移除了 {before_count} 项）")
    
    @task.command("list")
    async def task_list(self, event: AstrMessageEvent):
        """查看本群队列状态"""
        if event.is_private_chat():
            yield event.plain_result("此功能仅在群聊中可用")
            return
        
        group_id = self._get_group_id(event)
        
        waiting_count = len(self.waiting_queues.get(group_id, []))
        pending_count = len(self.pending_tasks_queues.get(group_id, []))
        active_count = len(self.active_tasks.get(group_id, {}))
        
        yield event.plain_result(
            f"📋 本群待命人员：{waiting_count}人\n"
            f"📢 本群待匹配任务：{pending_count}个\n"
            f"✅ 本群进行中任务：{active_count}个\n"
            f"⏰ 定时清除：{'开启' if self.auto_clear_enabled else '关闭'} ({self.clear_time})"
        )
    
    @task.command("set_clear")
    async def task_set_clear(self, event: AstrMessageEvent, time: str = None):
        """设置定时清除时间（管理员，全局设置）"""
        if not time:
            yield event.plain_result("请指定清除时间，例如：/task set_clear 23:59")
            return
        
        try:
            datetime.datetime.strptime(time, "%H:%M")
            self.clear_time = time
            self.auto_clear_enabled = True
            yield event.plain_result(f"✅ 已设置定时清除时间为每天 {time}（静默清除）")
        except:
            yield event.plain_result("❌ 时间格式错误，请使用 HH:MM 格式，例如 23:59")
    
    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听消息"""
        try:
            # 私聊不支持
            if event.is_private_chat():
                return
            
            message = event.message_str.strip()
            if not message:
                return
            
            group_id = self._get_group_id(event)
            user_id = self._get_user_id(event)
            
            logger.info(f"收到消息 - 群:{group_id} 用户:{user_id} 内容:{message}")
            
            # 初始化本群的数据结构
            if group_id not in self.waiting_queues:
                self.waiting_queues[group_id] = []
            if group_id not in self.pending_tasks_queues:
                self.pending_tasks_queues[group_id] = []
            if group_id not in self.active_tasks:
                self.active_tasks[group_id] = {}
            
            # ===== 1. 有人找活干（加入待命队列）- 严格匹配 =====
            join_keywords = ["找活干", "有无活", "有没有活", "🈶🈚🔥", "有无🔥", "🈶🈚活", "有无", "🈶🈚", "🈶无", "有🈚️","有活吗","有🔥吗","🈶🔥吗","🈶活吗"]
            if message.strip() in join_keywords:
                # 先检查本群是否有待匹配的任务
                if self.pending_tasks_queues[group_id]:
                    # 有任务在等待，直接匹配
                    task = self.pending_tasks_queues[group_id].pop(0)
                    
                    # 记录活跃任务
                    self.active_tasks[group_id][user_id] = {
                        "desc": task["desc"],
                        "start_time": datetime.datetime.now().timestamp()
                    }
                    
                    # 通知找活的人
                    chain1 = [
                        Comp.At(qq=user_id),
                        Comp.Plain("\n"),
                        Comp.Plain(f" 有任务了！{task['desc']}\n上号！！！")
                    ]
                    yield event.chain_result(chain1)
                    
                    # 通知发布任务的人
                    chain2 = [
                        Comp.At(qq=task["publisher"]),
                        Comp.Plain("\n"),
                        Comp.Plain(f" 有人接活了！{task['desc']}\n找活的人：{user_id}")
                    ]
                    yield event.chain_result(chain2)
                    
                    logger.info(f"群{group_id}任务匹配成功: 任务 {task['desc']} 由 {user_id} 接单")
                    return
                
                # 没有待匹配的任务，正常加入待命队列
                if user_id in self.waiting_queues[group_id]:
                    yield event.plain_result("您已经在等待队列中了")
                    return
                
                self.waiting_queues[group_id].append(user_id)
                position = len(self.waiting_queues[group_id])
                yield event.plain_result(f"✅ 您已加入待等待队列，当前第 {position} 位，20分钟内如果有任务的话就骚扰你")
                return
            
            # ===== 2. 有人发布任务（严格匹配格式）=====
            if self._is_task_message(message):
                logger.info(f"群{group_id}检测到任务发布: {message}")
                
                # 先检查本群是否有待命的人
                if self.waiting_queues[group_id]:
                    # 有待命的人，直接匹配
                    worker = self.waiting_queues[group_id].pop(0)
                    
                    # 记录活跃任务
                    self.active_tasks[group_id][worker] = {
                        "desc": message,
                        "start_time": datetime.datetime.now().timestamp()
                    }
                    
                    # 通知干活的人
                    chain1 = [
                        Comp.At(qq=worker),
                        Comp.Plain("\n"),
                        Comp.Plain(f" 有活儿了！{message}\n直接来⬆️吧⬇️！！")
                    ]
                    yield event.chain_result(chain1)
                    
                    # 通知发布任务的人
                    chain2 = [
                        Comp.At(qq=user_id),
                        Comp.Plain("\n"),
                        Comp.Plain(f" 已找到等活儿的人：{worker}\n任务：{message}")
                    ]
                    yield event.chain_result(chain2)
                    
                    logger.info(f"群{group_id}任务匹配成功: {message} 由 {worker} 接单")
                    return
                
                # 没有待命的人，将任务加入待匹配队列
                # 检查是否重复发布相同任务
                for task in self.pending_tasks_queues[group_id]:
                    if task["publisher"] == user_id:
                        yield event.plain_result("您已经发布过任务了，请等待匹配")
                        return
                
                self.pending_tasks_queues[group_id].append({
                    "desc": message,
                    "publisher": user_id,
                    "time": datetime.datetime.now().isoformat()
                })
                
                yield event.plain_result(
                    f"📢 任务已加入本群等待队列，当前有 {len(self.pending_tasks_queues[group_id])} 个任务在等待\n"
                    f"有人找活时会第一时间通知您"
                )
                return
            
            # ===== 3. 完成任务 =====
            if any(kw in message for kw in ["完成", "干完了", "结束"]):
                if user_id in self.active_tasks[group_id]:
                    task_info = self.active_tasks[group_id].pop(user_id)
                    elapsed = datetime.datetime.now().timestamp() - task_info["start_time"]
                    minutes = int(elapsed / 60)
                    yield event.plain_result(f"✅ 任务完成：{task_info['desc']}\n耗时：{minutes}分钟\n辛苦了！")
                    return
            
        except Exception as e:
            logger.error(f"处理消息出错: {e}")
            import traceback
            traceback.print_exc()