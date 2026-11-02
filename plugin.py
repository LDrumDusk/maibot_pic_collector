"""聊天图片收藏插件（MaiBot v2 SDK 插件）

功能概述：
- 通过 ``chat.receive.after_process`` Hook（OBSERVE 模式，只读、不阻塞主流程）捕获图片消息；
  主程序的图片识别是异步后台任务，此时可能尚未完成，插件会按 hash 轮询主库
  ``data/MaiBot.db`` 的 ``images`` 表等待识别文字（最多 ``filter.wait_description_sec`` 秒）。
  识别文字优先取消息段 ``data``（形如 ``[图片：描述]``），但主程序实际把 VLM 结果写入
  ``images.description``（纯图片消息段 ``data`` 通常为空），因此为空时按 hash 从主库读取。
- 用「文字 LLM（回复模型 replyer）」判断识别文字是否符合筛选条件，不耗视觉 token。
  筛选依据（``[filter]`` 配置节，可组合）：
  - persona：按 bot 人设（``personality.personality``）判断图片是否值得保存；
  - custom：按用户自定义要求（``custom_requirement``）判断；
  - both：两者任一命中（``match_logic=any``）或都命中（``match_logic=all``）。
- 符合条件的图片，保存「原图」（优先取消息段 ``binary_data_base64``，否则按 hash
  回退读取 ``data/images/<hash>.<ext>``），目标目录为 ``save_dir/YYYY-MM-DD/<hash>.<ext>``。
- 识别文字为空的图片直接跳过（无法判断，不保存）。
- 默认排除 bot 自己发的图片（对比 ``bot.qq_account`` 等）。
- 保存后随机选取一条预设通知文案（可配置、可关闭）发送。
- 提供聊天内指令配置（仅 ``[admin] command_whitelist`` 中 QQ 可用）：
  /收藏要求 <内容>、/收藏开关 <on|off>、/收藏统计。
- 订阅 bot 全局配置热重载，人设变化时自动刷新缓存。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import sqlite3
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode

# ==================== 常量 ====================

# 配置版本：与 _manifest.json 的 version 保持同步，用于配置结构迁移检测。
SUPPORTED_CONFIG_VERSION = "1.0.0"

# 图片 hash（sha256，64 位十六进制）匹配
_IMAGE_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

# 识别文字前缀，如 "[图片：一只猫]" / "[图片]"
_IMAGE_PREFIX_RE = re.compile(r"^\[图片(?:\s*[:：]\s*)?(.*?)\]$", re.S)

# 宿主图片库可能出现的扩展名（按 hash 回退探测）
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# 保存后的通知文案默认模板（支持 {date}、{reason}、{file} 占位符）
DEFAULT_NOTIFY_TEMPLATES = [
    "已收藏一张图片（{date}）：{reason}",
    "这张图我收下啦～ {reason}",
    "收藏 +1：{reason}",
    "已保存到收藏夹：{reason}",
]

# 默认人设占位（实际运行时从 bot 配置读取）
_DEFAULT_PERSONA = ""

# 命令权限拒绝提示
_CMD_DENIED_MSG = "你的 QQ 不在 [admin] command_whitelist 中，无法使用此指令。请先在插件配置里添加你的 QQ 号。"

# ==================== 配置模型 ====================


def _ui(label: str, hint: str = "") -> dict[str, Any]:
    """构造 json_schema_extra 的便捷函数（label/hint 显示在 WebUI）。"""
    extra: dict[str, Any] = {"label": label}
    if hint:
        extra["hint"] = hint
    return extra


class PluginSectionConfig(PluginConfigBase):
    """插件自身配置（[plugin] 配置节）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra=_ui("启用插件", "插件总开关"),
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步，用于检查配置是否需要更新）",
        json_schema_extra={
            **{"label": "配置版本", "hint": "配置版本，勿改"},
            "hidden": True,
            "disabled": True,
        },
    )


class FilterSectionConfig(PluginConfigBase):
    """筛选设置（[filter] 配置节）。"""

    __ui_label__ = "筛选"
    __ui_icon__ = "filter"
    __ui_order__ = 1

    selection_mode: Literal["persona", "custom", "both"] = Field(
        default="both",
        description="判断模式：persona=按人设，custom=按自定义要求，both=两者组合",
        json_schema_extra=_ui("判断模式", "persona 按人设；custom 按自定义要求；both 组合"),
    )
    custom_requirement: str = Field(
        default="",
        description="用户自定义的图片要求，例如：只保存猫咪、只保存二次元插画、只保存美食",
        json_schema_extra=_ui("自定义要求", "留空则仅依赖人设模式"),
    )
    match_logic: Literal["any", "all"] = Field(
        default="any",
        description="both 模式下的匹配逻辑：any=任一命中即保存，all=两者都命中才保存",
        json_schema_extra=_ui("匹配逻辑", "both 模式下生效"),
    )
    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群白名单（群号列表，为空表示不限群）",
        json_schema_extra=_ui("群白名单", "只有这些群会参与收藏；空=不限"),
    )
    group_blacklist: list[str] = Field(
        default_factory=list,
        description="群黑名单（群号列表，为空表示不排除）",
        json_schema_extra=_ui("群黑名单", "这些群不参与收藏；空=不排除"),
    )
    max_judge_per_minute: int = Field(
        default=60,
        description="每分钟最多调用 LLM 判断的次数（防刷限流）",
        json_schema_extra=_ui("每分钟判断上限", "防止图片刷屏时耗尽 LLM 配额"),
    )
    wait_description_sec: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        description="等待主程序图片识别完成的超时时间（秒）",
        json_schema_extra=_ui(
            "等待识别超时(秒)",
            "图片识别是异步后台任务，插件会轮询等待最多这么久再判定",
        ),
    )


class StorageSectionConfig(PluginConfigBase):
    """保存设置（[storage] 配置节）。"""

    __ui_label__ = "保存"
    __ui_icon__ = "folder"
    __ui_order__ = 2

    save_dir: str = Field(
        default="",
        description="图片保存目录（绝对路径，如 D:/pics）；留空则保存到插件数据目录 collected/ 下",
        json_schema_extra=_ui("保存目录", "绝对路径；留空用插件数据目录 collected/"),
    )
    notify_enabled: bool = Field(
        default=True,
        description="保存后是否发送通知",
        json_schema_extra=_ui("保存后通知", "开关通知"),
    )
    notify_templates: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NOTIFY_TEMPLATES),
        description="通知文案模板列表（随机选一条；占位符 {date}、{reason}、{file}）",
        json_schema_extra=_ui("通知文案", "每行一条，随机发送"),
    )
    exclude_bot: bool = Field(
        default=True,
        description="排除 bot 自己发的图片",
        json_schema_extra=_ui("排除麦麦自己发的图", "默认开启"),
    )


class AdminSectionConfig(PluginConfigBase):
    """管理员设置（[admin] 配置节）。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    command_whitelist: list[str] = Field(
        default_factory=list,
        description="允许使用聊天内指令配置（/收藏要求 等）的 QQ 号列表",
        json_schema_extra=_ui("指令白名单", "只有这些 QQ 能用指令改配置；空=无人能用"),
    )


class PicCollectorConfig(PluginConfigBase):
    """插件配置根模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    filter: FilterSectionConfig = Field(default_factory=FilterSectionConfig)
    storage: StorageSectionConfig = Field(default_factory=StorageSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)


# ==================== 工具函数 ====================


def sniff_image_ext(data: bytes) -> str:
    """按魔数判断图片扩展名；未识别时默认 .jpg。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".jpg"


def strip_image_prefix(data: str) -> str:
    """去掉识别文字段里的 [图片：xxx] / [图片] 前缀，返回纯描述文本。"""
    text = (data or "").strip()
    if not text:
        return ""
    m = _IMAGE_PREFIX_RE.match(text)
    if m:
        return (m.group(1) or "").strip()
    return text


def extract_plain_id(value: Any) -> str:
    """把 QQ 号归一化为纯数字（兼容 'qq:10001' 等平台前缀）。"""
    text = str(value or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text.strip()


# ==================== 插件主体 ====================


class PicCollectorPlugin(MaiBotPlugin):
    """聊天图片收藏插件。"""

    config_model = PicCollectorConfig
    # 订阅 bot 全局配置热重载：人设（personality）变化时刷新缓存
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._personality_cache: str | None = None
        self._bot_ids_cache: set[str] | None = None
        self._seen_records: list[dict[str, Any]] = []
        self._judge_times: deque[float] = deque()
        self._criteria_missing_logged = False
        self._save_lock = asyncio.Lock()  # 序列化保存+元数据更新，防止并发写

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        data_dir = self._data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._seen_records = self._load_metadata()
        self._personality_cache = None
        self._bot_ids_cache = None
        self.ctx.logger.info(
            "聊天图片收藏插件已加载：模式=%s，已收藏 %d 张，保存目录=%s",
            self.config.filter.selection_mode,
            len(self._seen_records),
            self._resolve_save_dir(),
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("聊天图片收藏插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            self.ctx.logger.info("收藏插件配置已更新")
        elif scope == "bot":
            # 人设等 bot 配置变化：清空缓存，下次读取时重新加载
            self._personality_cache = None
            self._bot_ids_cache = None
            self.ctx.logger.debug("收藏插件：bot 配置已重载，已刷新人设/账号缓存")

    # ==================== 路径与状态 ====================

    def _data_dir(self) -> Path:
        return Path(self.ctx.paths.data_dir)

    def _metadata_path(self) -> Path:
        return self._data_dir() / "metadata.json"

    def _resolve_save_dir(self) -> Path:
        """返回保存目录：优先配置 save_dir，否则回退到插件数据目录 collected/。"""
        raw = str(self.config.storage.save_dir or "").strip()
        if raw:
            path = Path(raw).expanduser()
        else:
            path = self._data_dir() / "collected"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.ctx.logger.warning("无法创建保存目录 %s：%s", path, exc)
        return path

    def _load_metadata(self) -> list[dict[str, Any]]:
        path = self._metadata_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                records = data.get("images", [])
            elif isinstance(data, list):
                records = data
            else:
                return []
            return [r for r in records if isinstance(r, dict)]
        except Exception as exc:
            self.ctx.logger.warning("读取元数据失败：%s", exc)
            return []

    async def _save_metadata(self) -> None:
        path = self._metadata_path()
        snapshot = list(self._seen_records)

        def _write() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"images": snapshot}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                self.ctx.logger.warning("写入元数据失败：%s", exc)

        await asyncio.to_thread(_write)

    def _metadata_has(self, image_hash: str) -> bool:
        return any(str(r.get("hash") or "") == image_hash for r in self._seen_records)

    # ==================== 全局配置读取（带缓存） ====================

    async def _get_personality(self) -> str:
        if self._personality_cache is None:
            try:
                value = await self.ctx.config.get("personality.personality", "")
                self._personality_cache = str(value or "").strip()
            except Exception as exc:
                self.ctx.logger.warning("读取人设失败：%s", exc)
                self._personality_cache = ""
        return self._personality_cache

    async def _get_bot_ids(self) -> set[str]:
        if self._bot_ids_cache is None:
            ids: set[str] = set()
            try:
                qq = await self.ctx.config.get("bot.qq_account", "")
                if qq:
                    ids.add(extract_plain_id(qq))
            except Exception as exc:
                self.ctx.logger.debug("读取 bot.qq_account 失败：%s", exc)
            try:
                platforms = await self.ctx.config.get("bot.platforms", [])
                for item in platforms or []:
                    if isinstance(item, str) and ":" in item:
                        ids.add(extract_plain_id(item.split(":", 1)[1]))
            except Exception as exc:
                self.ctx.logger.debug("读取 bot.platforms 失败：%s", exc)
            self._bot_ids_cache = ids
        return self._bot_ids_cache

    # ==================== 筛选条件 ====================

    def _get_custom_requirement(self) -> str:
        return str(self.config.filter.custom_requirement or "").strip()

    def _has_criteria(self) -> bool:
        """判断当前是否具备可用的筛选依据。"""
        mode = self.config.filter.selection_mode
        personality = self._personality_cache if self._personality_cache is not None else ""
        custom = self._get_custom_requirement()
        if mode in ("persona", "both") and personality.strip():
            return True
        if mode in ("custom", "both") and custom:
            return True
        return False

    def _build_judge_prompt(self, description: str, personality: str) -> str:
        mode = self.config.filter.selection_mode
        custom = self._get_custom_requirement()
        persona_block = ""
        custom_block = ""
        if mode in ("persona", "both") and personality.strip():
            persona_block = f"【人设】\n{personality}\n"
        if mode in ("custom", "both") and custom:
            custom_block = f"【自定义要求】\n{custom}\n"
        if mode == "both":
            logic = "任一命中即保存" if self.config.filter.match_logic == "any" else "两个都要命中才保存"
            logic_text = f"both 模式：{logic}。"
        else:
            logic_text = f"{mode} 模式：仅依据该模式判断。"

        return (
            "你是图片收藏筛选器。请根据【图片识别文字】判断这张图片是否应该被收藏保存。\n"
            "只能依据识别文字判断图片内容，不要臆测、不要脑补画面里没有的东西。\n\n"
            f"【图片识别文字】\n{description}\n\n"
            f"{persona_block}"
            f"{custom_block}"
            "【判断说明】\n"
            f"- {logic_text}\n"
            "- 人设模式下：识别文字描述的内容如果符合人设的偏好、气质、话题，则视为值得保存；"
            "如果明显不符合或无关，则不保存。\n"
            "- 自定义模式下：识别文字描述的内容如果满足自定义要求，则视为值得保存。\n\n"
            "【输出要求】\n"
            "只返回一个 JSON 对象，不要输出任何其他文字、不要用 Markdown 代码块：\n"
            '{"save": true 或 false, "reason": "一句话说明判断依据"}'
        )

    def _parse_llm_verdict(self, raw: str) -> tuple[bool, str]:
        """解析 LLM 返回的 JSON 判定，容错处理代码块/多余文字。返回 (是否保存, 理由)。"""
        text = (raw or "").strip()
        # 去掉 ```json ... ``` 围栏
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence:
            text = fence.group(1).strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict) and "save" in data:
                    return bool(data.get("save")), str(data.get("reason") or "").strip()
            except json.JSONDecodeError:
                pass
        # 兜底：包含明显的是/true
        lower = text.lower()
        if "true" in lower or "是" in text:
            return True, text[:80]
        return False, ""

    async def _judge_with_llm(self, description: str) -> tuple[bool, str]:
        """调用回复模型（文字 LLM）判断识别文字是否符合条件。返回 (是否保存, 理由)。"""
        personality = await self._get_personality()
        if not self._has_criteria():
            return False, ""
        prompt = self._build_judge_prompt(description, personality)
        try:
            result = await self.ctx.llm.generate(prompt, task_name="replyer")
        except Exception as exc:
            self.ctx.logger.warning("收藏判断 LLM 调用异常：%s", exc)
            return False, ""
        if not result.get("success"):
            self.ctx.logger.warning(
                "收藏判断 LLM 调用失败：%s",
                result.get("reasoning") or result.get("response") or "unknown",
            )
            return False, ""
        return self._parse_llm_verdict(str(result.get("response") or ""))

    # ==================== 限流 ====================

    def _acquire_judge_slot(self) -> bool:
        """简单的滑动窗口限流，防止图片刷屏时耗尽 LLM 配额。"""
        limit = max(1, int(self.config.filter.max_judge_per_minute))
        now = time.time()
        while self._judge_times and now - self._judge_times[0] > 60:
            self._judge_times.popleft()
        if len(self._judge_times) >= limit:
            return False
        self._judge_times.append(now)
        return True

    # ==================== 消息解析 ====================

    def _extract_sender_id(self, message: dict[str, Any]) -> str:
        info = message.get("message_info") or {}
        user_info = info.get("user_info") or {}
        uid = str(user_info.get("user_id") or "").strip()
        if uid:
            return uid
        sender = message.get("sender") or {}
        uid = str(sender.get("user_id") or "").strip()
        if uid:
            return uid
        return str(message.get("user_id") or "").strip()

    def _extract_group_id(self, message: dict[str, Any]) -> str:
        info = message.get("message_info") or {}
        group_info = info.get("group_info") or {}
        return str(group_info.get("group_id") or "").strip()

    def _extract_stream_id(self, message: dict[str, Any]) -> str:
        return str(message.get("stream_id") or message.get("session_id") or "").strip()

    def _is_group_allowed(self, group_id: str) -> bool:
        # 归一化为字符串，兼容配置里可能出现的 int 群号
        wl = [str(q) for q in (self.config.filter.group_whitelist or [])]
        bl = [str(q) for q in (self.config.filter.group_blacklist or [])]
        if group_id:
            if wl and group_id not in wl:
                return False
            if bl and group_id in bl:
                return False
        return True

    async def _is_bot_message(self, message: dict[str, Any]) -> bool:
        if not self.config.storage.exclude_bot:
            return False
        sender = extract_plain_id(self._extract_sender_id(message))
        if not sender:
            return False
        bot_ids = await self._get_bot_ids()
        return sender in bot_ids

    def _iter_image_segments(self, message: dict[str, Any]):
        for seg in message.get("raw_message") or []:
            if isinstance(seg, dict) and str(seg.get("type") or "") == "image":
                yield seg

    # ==================== 图片读取与保存 ====================

    def _resolve_main_db_path(self) -> Path:
        """主库路径：data/plugins/<id> 的上上级 data/MaiBot.db。"""
        return Path(self.ctx.paths.data_dir).parent.parent / "MaiBot.db"

    def _read_image_description_sync(self, db_path: Path, image_hash: str) -> str:
        """从主库 images 表按 hash 读识别文字（只读连接，WAL 模式下安全）。"""
        if not db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT description FROM images WHERE image_hash = ?",
                    (image_hash,),
                )
                row = cur.fetchone()
                return str(row[0] or "") if row else ""
            finally:
                conn.close()
        except Exception as exc:
            self.ctx.logger.debug("读取图片识别文字失败：%s", exc)
            return ""

    async def _get_image_description(self, seg: dict[str, Any], image_hash: str) -> str:
        """取识别文字：优先消息段 data（去掉 [图片：] 前缀），为空则按 hash 从主库读取。

        主程序把 VLM 识别结果写入 ``images.description``，但图片理解是**异步后台任务**，
        在 ``chat.receive.after_process``（HeartFlow 阶段之前）触发时通常尚未完成。
        因此这里按 hash 轮询主库，最多等待 ``filter.wait_description_sec`` 秒；
        OBSERVE 模式下本协程是独立后台任务，等待不会阻塞主流程。

        返回空字符串表示超时仍无识别文字（调用方应跳过，不保存）。
        """
        desc = strip_image_prefix(str(seg.get("data") or ""))
        if desc:
            return desc
        db_path = self._resolve_main_db_path()
        timeout = max(1.0, float(self.config.filter.wait_description_sec))
        deadline = time.monotonic() + timeout
        while True:
            desc = await asyncio.to_thread(
                self._read_image_description_sync, db_path, image_hash
            )
            if desc:
                return desc
            if time.monotonic() >= deadline:
                return ""
            await asyncio.sleep(0.5)

    @staticmethod
    def _read_host_image_bytes(host_data_dir: Path, image_hash: str) -> bytes | None:
        """按宿主图片库落盘约定探测 <data>/images/<hash>.<ext>。"""
        for ext in _IMAGE_EXTS:
            path = host_data_dir / "images" / f"{image_hash}{ext}"
            try:
                if path.is_file():
                    return path.read_bytes()
            except OSError:
                continue
        return None

    async def _resolve_image_bytes(self, seg: dict[str, Any], image_hash: str) -> bytes | None:
        """取图片二进制：优先消息段 binary_data_base64，其次按 hash 回退宿主图片库。"""
        b64 = str(seg.get("binary_data_base64") or "").strip()
        if b64:
            try:
                return base64.b64decode(b64)
            except Exception:
                self.ctx.logger.debug("图片段 base64 解码失败，尝试回退宿主图片库")
        host_data_dir = Path(self.ctx.paths.data_dir).parent.parent
        return await asyncio.to_thread(self._read_host_image_bytes, host_data_dir, image_hash)

    async def _save_image(
        self,
        image_hash: str,
        data: bytes,
        description: str,
        reason: str,
        sender_id: str,
        group_id: str,
    ) -> str | None:
        """保存原图并按日期分文件夹，返回保存后的绝对路径；重复/失败返回 None。

        用锁串行化「查重 + 写文件 + 追加元数据」，防止并发消息互相覆盖。
        """
        save_dir = self._resolve_save_dir()
        ext = sniff_image_ext(data)
        date_str = datetime.now().strftime("%Y-%m-%d")
        day_dir = save_dir / date_str
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.ctx.logger.error("创建日期目录失败 %s：%s", day_dir, exc)
            return None

        dest = day_dir / f"{image_hash}{ext}"
        async with self._save_lock:
            if dest.exists() or self._metadata_has(image_hash):
                return None

            try:
                await asyncio.to_thread(dest.write_bytes, data)
            except OSError as exc:
                self.ctx.logger.error("保存图片失败 %s：%s", dest, exc)
                return None

            record = {
                "hash": image_hash,
                "file": str(dest),
                "date": date_str,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "source_group": group_id,
                "source_user": sender_id,
                "description": description[:500],
                "reason": (reason or "")[:200],
            }
            self._seen_records.append(record)
            await self._save_metadata()
        return str(dest)

    async def _notify(self, stream_id: str, date_str: str, reason: str, file: str) -> None:
        if not stream_id or not self.config.storage.notify_enabled:
            return
        templates = self.config.storage.notify_templates or DEFAULT_NOTIFY_TEMPLATES
        if not templates:
            templates = DEFAULT_NOTIFY_TEMPLATES
        text = random.choice(templates).format(
            date=date_str,
            reason=(reason or "符合筛选条件")[:80],
            file=Path(file).name,
        )
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("发送收藏通知失败：%s", exc)

    # ==================== 指令权限 ====================

    async def _is_command_authorized(self, message: dict[str, Any]) -> bool:
        sender = extract_plain_id(self._extract_sender_id(message))
        whitelist = [extract_plain_id(q) for q in (self.config.admin.command_whitelist or [])]
        return bool(sender and sender in whitelist)

    async def _persist_config_field(self, section: str, field: str, value: Any) -> bool:
        """把某个配置字段写回插件目录 config.toml，交由 Runner 热重载。"""
        config_path = os.path.join(self._plugin_dir, "config.toml")
        try:
            import tomlkit

            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    doc = tomlkit.parse(f.read())
            else:
                doc = tomlkit.document()
            section_table = doc.setdefault(section, tomlkit.table())
            section_table[field] = value
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(tomlkit.dumps(doc))
            return True
        except Exception as exc:
            self.ctx.logger.warning("写入 config.toml 失败：%s", exc)
            return False

    def _apply_in_memory_config(self, section: str, field: str, value: Any) -> None:
        """把某个配置字段立即应用到内存配置（不等 Runner 热重载）。"""
        try:
            raw = self.get_plugin_config_data()
            raw.setdefault(section, {})[field] = value
            self.set_plugin_config(raw)
        except Exception as exc:
            self.ctx.logger.debug("刷新内存配置失败：%s", exc)

    # ==================== 主 Hook：捕获图片并收藏 ====================

    @HookHandler(
        "chat.receive.after_process",
        name="pic_collect",
        mode=HookMode.OBSERVE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hook_pic_collect(self, **kwargs: Any):
        """在消息处理完成后观察图片消息，后台判断并保存符合条件的原图。

        OBSERVE 模式：返回值被忽略，不影响主流程。识别文字此时已就绪。
        """
        if not self.config.plugin.enabled:
            return None

        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            return None
        if message.get("is_notify"):
            return None

        # 排除 bot 自己发的图
        if await self._is_bot_message(message):
            return None

        group_id = self._extract_group_id(message)
        if not self._is_group_allowed(group_id):
            return None

        # 确保人设缓存已加载，再判断是否有可用筛选依据（否则首次会误判无标准）
        await self._get_personality()
        if not self._has_criteria():
            if not self._criteria_missing_logged:
                self._criteria_missing_logged = True
                self.ctx.logger.warning(
                    "收藏筛选无可用依据：selection_mode=%s 但人设/自定义要求均为空，本次跳过所有图片",
                    self.config.filter.selection_mode,
                )
            return None

        sender_id = self._extract_sender_id(message)
        stream_id = self._extract_stream_id(message)
        saved_any = False
        last_reason = ""
        last_file = ""
        date_str = datetime.now().strftime("%Y-%m-%d")

        image_segments = list(self._iter_image_segments(message))
        if not image_segments:
            return None
        self.ctx.logger.info(
            "图片收藏：收到 %d 个图片段（群=%s，发送者=%s，等待识别最多 %.0f 秒）",
            len(image_segments), group_id or "-", sender_id or "-",
            max(1.0, float(self.config.filter.wait_description_sec)),
        )

        for seg in image_segments:
            image_hash = str(seg.get("hash") or "").strip().lower()
            if not _IMAGE_HASH_RE.match(image_hash):
                self.ctx.logger.debug("图片段 hash 缺失或异常，跳过")
                continue
            if self._metadata_has(image_hash):
                self.ctx.logger.debug("图片 %s 已收藏过，跳过", image_hash[:12])
                continue

            # 识别文字：优先段 data，其次按 hash 从主库 images 表轮询等待；超时仍空则跳过
            description = await self._get_image_description(seg, image_hash)
            if not description:
                self.ctx.logger.info(
                    "图片 %s 识别文字等待超时（%.0f 秒）仍为空，跳过（不保存）",
                    image_hash[:12], max(1.0, float(self.config.filter.wait_description_sec)),
                )
                continue

            if not self._acquire_judge_slot():
                self.ctx.logger.warning("收藏判断触发限流，跳过图片 %s", image_hash[:12])
                continue

            should_save, reason = await self._judge_with_llm(description)
            if not should_save:
                self.ctx.logger.debug("图片 %s 未命中筛选条件：%s", image_hash[:12], reason or "无理由")
                continue

            data = await self._resolve_image_bytes(seg, image_hash)
            if not data:
                self.ctx.logger.warning("图片 %s 取不到二进制数据，跳过", image_hash[:12])
                continue

            saved_path = await self._save_image(
                image_hash, data, description, reason, sender_id, group_id
            )
            if saved_path:
                saved_any = True
                last_reason = reason
                last_file = saved_path
                self.ctx.logger.info(
                    "已收藏图片 %s（群=%s，发送者=%s，理由=%s）-> %s",
                    image_hash[:12], group_id or "-", sender_id or "-", reason or "-", saved_path,
                )

        if saved_any:
            await self._notify(stream_id, date_str, last_reason, last_file)

        return None

    # ==================== 聊天内指令配置 ====================

    @Command(
        "pic_collector_set_requirement",
        description="设置图片收藏的筛选要求（仅白名单 QQ）",
        pattern=r"^/?收藏要求\s*(?P<req>[\s\S]*)$",
    )
    async def cmd_set_requirement(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not await self._is_command_authorized(message):
            await self.ctx.send.text(_CMD_DENIED_MSG, stream_id)
            return False, "无权限", 1

        matched = kwargs.get("matched_groups") or {}
        req = str(matched.get("req") or "").strip()
        if not req:
            await self.ctx.send.text(
                "用法：/收藏要求 <内容>\n例如：/收藏要求 只保存猫咪\n用 /收藏要求 清空 清除要求",
                stream_id,
            )
            return False, "缺少参数", 1
        if req.lower() in ("清空", "清除", "无", "空"):
            req = ""

        ok = await self._persist_config_field("filter", "custom_requirement", req)
        # 立即生效到内存（不等热重载）
        self._apply_in_memory_config("filter", "custom_requirement", req)
        text = (
            "已设置收藏筛选要求：" + (req or "（已清空）")
            if ok
            else "写入 config.toml 失败，本次仅内存生效：" + (req or "（已清空）")
        )
        await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command(
        "pic_collector_set_enabled",
        description="开启/暂停图片收藏（仅白名单 QQ）",
        pattern=r"^/?收藏开关\s*(?P<state>on|off|开|关|启用|停用|暂停)?\s*$",
    )
    async def cmd_set_enabled(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not await self._is_command_authorized(message):
            await self.ctx.send.text(_CMD_DENIED_MSG, stream_id)
            return False, "无权限", 1

        matched = kwargs.get("matched_groups") or {}
        state = str(matched.get("state") or "").strip().lower()
        if not state:
            current = "开启" if self.config.plugin.enabled else "关闭"
            await self.ctx.send.text(f"当前状态：{current}。用法：/收藏开关 on|off", stream_id)
            return True, f"当前：{current}", 1
        new_state = state in ("on", "开", "启用")
        ok = await self._persist_config_field("plugin", "enabled", new_state)
        self._apply_in_memory_config("plugin", "enabled", new_state)
        text = f"图片收藏已{'开启' if new_state else '关闭'}" + ("" if ok else "（config.toml 写入失败，仅本次生效）")
        await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command(
        "pic_collector_stats",
        description="查看图片收藏统计（仅白名单 QQ）",
        pattern=r"^/?收藏统计\s*$",
    )
    async def cmd_stats(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not await self._is_command_authorized(message):
            await self.ctx.send.text(_CMD_DENIED_MSG, stream_id)
            return False, "无权限", 1

        records = self._seen_records
        total = len(records)
        by_date: dict[str, int] = {}
        for r in records:
            d = str(r.get("date") or "未知")
            by_date[d] = by_date.get(d, 0) + 1

        mode = self.config.filter.selection_mode
        custom = self._get_custom_requirement()
        enabled = self.config.plugin.enabled

        lines = [
            f"图片收藏统计（共 {total} 张）",
            f"状态：{'开启' if enabled else '关闭'}，模式：{mode}",
        ]
        if custom:
            lines.append(f"自定义要求：{custom}")
        if by_date:
            lines.append("按日期：")
            for d in sorted(by_date, reverse=True):
                lines.append(f"  {d}：{by_date[d]} 张")
        else:
            lines.append("暂无收藏记录")
        lines.append(f"保存目录：{self._resolve_save_dir()}")

        text = "\n".join(lines)
        await self.ctx.send.text(text, stream_id)
        return True, text, 2


# ==================== 插件入口 ====================


def create_plugin() -> PicCollectorPlugin:
    """SDK 插件工厂函数。"""
    return PicCollectorPlugin()
