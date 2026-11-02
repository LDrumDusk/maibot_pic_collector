# 聊天图片收藏（maibot_pic_collector）

MaiBot（麦麦）第三方插件：**有选择性地保存聊天记录图片**。

- 判断方式：使用主程序已生成的**图片识别文字**（优先消息段 `data`，为空则按 hash 从主库 `images.description` 读取），交给**文字 LLM（回复模型 replyer）**判断是否符合条件，**不消耗视觉 token**。
- 保存内容：**原图**（优先取消息段 `binary_data_base64`，否则按 hash 回退读取 `data/images/`）。
- 保存位置：用户可配置的 `save_dir`，按 **`YYYY-MM-DD/` 日期分文件夹**，文件名 `<图片hash>.<扩展名>`（扩展名按魔数嗅探）。
- 筛选依据（可组合）：
  - `persona`：按 bot 人设（`personality.personality`）判断；
  - `custom`：按用户自定义要求判断；
  - `both`：两者组合（`match_logic=any` 任一命中 / `all` 都命中）。
- 只保存**用户**发的图（默认排除 bot 自己发的，可配置）。
- 识别文字为空的图**直接跳过**，不保存。
- 保存后**随机发送一条预设通知**（文案可自定义、可关闭）。

---

## 安装

1. 把整个 `maibot_pic_collector` 文件夹复制到麦麦的 `modules/MaiBot/plugins/` 下。
2. 在麦麦 WebUI 的「插件」页找到「聊天图片收藏」并启用，或直接重启麦麦。
3. 首次加载会自动生成 `config.toml`（默认配置）。

> 开发调试：本插件在独立目录开发，`plugin.py` 与 `_manifest.json` 在同一目录即可加载；配置写入会作用于该目录下的 `config.toml`。

---

## 配置说明（config.toml）

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[filter]
# persona=按人设；custom=按自定义要求；both=两者组合
selection_mode = "both"
# 用户自定义要求，例如：只保存猫咪、只保存二次元插画、只保存美食
custom_requirement = ""
# both 模式下的匹配逻辑：any=任一命中即保存；all=两个都命中才保存
match_logic = "any"
# 群白名单（空=不限群）
group_whitelist = []
# 群黑名单（空=不排除）
group_blacklist = []
# 每分钟最多调用 LLM 判断次数（防刷限流）
max_judge_per_minute = 60
# 等待主程序图片识别完成的超时时间（秒）；图片识别是异步任务，插件会轮询等待
wait_description_sec = 30.0

[storage]
# 图片保存目录（绝对路径，如 "D:/pics"）；留空则保存到插件数据目录 collected/ 下
save_dir = ""
# 保存后是否发送通知
notify_enabled = true
# 通知文案模板列表（随机选一条；占位符 {date} {reason} {file}）
notify_templates = [
  "已收藏一张图片（{date}）：{reason}",
  "这张图我收下啦～ {reason}",
  "收藏 +1：{reason}",
  "已保存到收藏夹：{reason}",
]
# 排除 bot 自己发的图片
exclude_bot = true

[admin]
# 允许使用聊天内指令配置的 QQ 号列表（空=无人能用，fail-closed）
command_whitelist = []
```

### 说明

- `save_dir` 留空时保存到插件数据目录 `data/plugins/com.maimai.pic-collector/collected/`（随项目走，免配置即可用）；填绝对路径则保存到指定目录。
- `group_whitelist` / `group_blacklist` 只在群聊时生效；私聊消息不受群名单影响。
- `max_judge_per_minute` 防止图片刷屏时频繁调用 LLM 判断。
- `wait_description_sec`：主程序的图片识别是**异步后台任务**，`chat.receive.after_process` 触发时可能还没完成；插件会按 hash 轮询主库 `data/MaiBot.db` 的 `images` 表等待识别文字，最多等这么久（默认 30 秒）。超时仍无识别文字则跳过该图（不保存）。
- 人设、bot 账号读取自主程序 `bot_config.toml`（`personality.personality`、`bot.qq_account`、`bot.platforms`），变化时插件自动刷新。

---

## 聊天内指令（仅白名单 QQ）

| 指令 | 说明 |
|---|---|
| `/收藏要求 <内容>` | 设置自定义筛选要求；`/收藏要求 清空` 清除 |
| `/收藏开关 on\|off` | 开启 / 暂停收藏 |
| `/收藏统计` | 查看已收藏数量、按日期分布、保存目录 |

> `[admin] command_whitelist` 需先填好你的 QQ 号才能使用这些指令（fail-closed）。指令修改会写回插件目录的 `config.toml`，可热重载。

---

## 判断流程（技术细节）

```
chat.receive.after_process（OBSERVE，只读，不阻塞）
  → 遍历 raw_message 中的 image 段
  → 过滤：排除 bot 自己发的 / 群白黑名单 / hash 去重
  → 取识别文字 = 段 data 去掉 "[图片：]" 前缀；若为空则按 hash 从主库 images.description 读取；仍为空 → 跳过
  → ctx.llm.generate(判断prompt, task_name="replyer") 要求返回 {"save":bool,"reason":str}
  → save=true → 取原图二进制 → 魔数嗅探扩展名
  → 写入 save_dir/YYYY-MM-DD/<hash>.<ext> + 写元数据 metadata.json
  → 随机通知文案发送
```

- 识别文字真实来源：主程序把 VLM 识别结果写入 `images.description`，纯图片消息段的 `data` 通常为空；插件按 hash 只读查询主库 `data/MaiBot.db` 取回（无需额外 capability）。
- 判断 prompt 内联了人设（`personality.personality`）与自定义要求，要求 LLM 只返回 JSON，解析失败则按「不保存」处理。
- 元数据（hash、来源群/用户、保存时间、识别文字、理由、保存路径）记录在插件数据目录 `metadata.json`，用于去重与统计。

---

## 依赖

- `tomlkit`（>=0.13.0）：聊天内指令修改 `config.toml` 用。

## 开源协议

MIT
