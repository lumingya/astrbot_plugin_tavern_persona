# 角色卡转换器（兼容 SillyTavern 格式的 AstrBot 插件）

在不修改 AstrBot 源码的前提下，将兼容 SillyTavern V2/V3 格式的 PNG 或 JSON 角色卡导入 AstrBot，转换为当前会话使用的人设。这里的 “SillyTavern” 指角色卡格式和生态兼容性，不是运行依赖；插件不会安装、启动或扫描 SillyTavern。

人设通过 ProviderRequest.add_system_prompt(..., slot=PERSONA) 注入；未选择卡片的会话不受影响，状态持久化并按会话隔离。

## 安装

将本插件目录放到 AstrBot 的插件目录，在 AstrBot 插件页启用并重载。仅使用 Python 标准库，不需要安装 SillyTavern 或额外依赖。

推荐通过 AstrBot 管理页面的“选择文件”或拖放方式导入你电脑上的 PNG/JSON 角色卡；浏览器会把所选文件上传到 AstrBot。

聊天命令在 AstrBot 主机上运行，只能读取该主机可访问的路径。例如：

    /card import /srv/astrbot/cards/your-character.png
    /card import /srv/astrbot/cards/your-character.json

如果希望使用短文件名，可在插件配置中手动填写可选的 source_dir；插件只会在该目录下解析相对路径，不会自动猜测或扫描任何外部角色卡目录。升级前若曾设置过旧版默认值，请在插件设置中将 source_dir 清空，或改成你实际希望使用的目录。

导入后的规范化数据保存在 AstrBot 的插件数据目录 plugin_data/astrbot_plugin_tavern_persona/cards/，原始 PNG 不会复制。

## 独立管理页面

在 AstrBot 管理面板的插件卡片中打开“页面”即可进入 console 管理页。页面通过 AstrBot Plugin Page Bridge 调用插件 API，支持：

- 拖放或选择 PNG/JSON 角色卡导入；页面会按小块顺序上传，不把整张文件编码成一个超大 JSON 请求；
- 分块上传单文件上限 128 MiB、最多同时保留 2 个上传会话；中断上传的 30 分钟 TTL 采用惰性回收（下一次上传 API 调用或插件初始化时清理），不是后台定时任务。已完成导入的 finish 重放结果单独保留 10 分钟、最多 128 条，缓存响应总量不超过 4 MiB；Web 返回的角色名最多 256 个字符，warnings 也有条数和单条长度上限。
- 页面按小块发送可避免本插件 UI 生成单个超大 JSON；但当前 Dashboard adapter 会在插件 handler 前读取完整请求体，因此插件侧大小校验不能限制恶意或非页面客户端直发的超大 HTTP body。该入口限制需要由 ASGI/代理层处理，本插件不修改 AstrBot core。
- 查看角色核心字段、开场白和世界书条目，并可直接在线编辑原生 Persona ID（支持自定义重命名并自动同步至已关联会话，或一键恢复默认格式）；
- 在聊天中运行 /card status（兼容 /stcard status），复制输出的“会话 ID”，再于页面粘贴以启用或关闭角色卡、切换世界书模式；
- 在详情页审阅 Regex Scripts 和其他扩展：内置 Regex Scripts 会按配置执行；TavernHelper 等任意 JavaScript 扩展只保留和展示，不会执行。

## 命令

- /card import <PNG或JSON路径>：读取卡片并导入
- /card list：列出导入卡片与原生 Persona
- /card use <编号或名称>：为当前 UMO 的全部对话启用该 Persona
- /card off：关闭当前 UMO 的全部对话 Persona
- /card status：查看当前状态
- /card help：列出全部快捷命令
- /card persona [卡片] [新ID/reset]：查看或修改角色卡的原生 Persona ID（传入 reset 可恢复默认）
- /card regex [卡片]：查看卡片 Regex Scripts
- /card worldbook triggered|all|off：选择关键词触发、全部启用条目或关闭世界书
- /card greetings [卡片]：列出默认开场白和所有备用开场白
- /card greet <编号> [卡片]：将所选开场白直接作为命令回复发送到当前会话，不额外触发 LLM
- /card scripts [卡片]：将脚本/正则数据导出至 plugin_data/astrbot_plugin_tavern_persona/exports/；内置 Regex Scripts 会按配置执行，其他扩展代码不执行

命令同时接受 /stcard 兼容别名；名称有重名时用 /card list 显示的编号选择。

## 字段映射与安全处理

- description、personality、scenario、system_prompt、post_history_instructions、extensions.depth_prompt.prompt、可选 mes_example 映射到人设；保留 {{char}}/{{user}} 的常见占位符替换。
- 默认世界书采用关键词触发：匹配 keys，读取 secondary_keys 和 selectiveLogic，只注入启用条目和 constant 常驻条目；排序按 insertion_order。默认最多 80 条、32,000 字符。世界书不会执行 use_regex 触发条件，命令 /stcard worldbook all 可将其内容作为普通文本纳入已启用条目。
- 角色卡 regex_scripts 会按 placement=1（用户输入）和 placement=2（AI 输出）执行 findRegex/replaceString；支持 flags、capture groups、{{match}}、{{char}}/{{user}}、trimStrings 与 promptOnly。promptOnly 只作用于提示词阶段，不改写展示或历史文本。
- TavernHelper/其他任意 JavaScript 扩展仍原样保留在卡片 JSON，不会执行；/card scripts 导出脚本/正则相关扩展供人工查看。
- PNG 读取 ccv3（优先）和 chara 文本块，兼容 tEXt、zTXt、iTXt；校验 PNG 块 CRC 和文件/解压大小上限。
- 人设仅按 unified_msg_origin 会话注入，不会写回 AstrBot 的全局配置或改变 AstrBot 对话设置。

## 已知转换边界

AstrBot 提示词不是 SillyTavern 的完整运行时：本插件仅实现角色卡 Regex Scripts 的有限 findRegex/replaceString 处理，不模拟 TavernHelper、宏系统、世界书递归/概率/冷却/深度扫描，也不会运行卡片中的任意 JavaScript 代码。默认世界书只扫描当前请求提示词及最近用户上下文中的文本，属于有界的兼容实现；未匹配到的条目不会注入。卡片内图片/音频资源不映射为提示词。

## 测试

在插件目录运行：

    $env:PYTHONPATH = (Get-Location).Path
    python -m unittest discover -s tests -v

测试覆盖 V2/V3 解析、PNG 文本块、CRC 错误、宏替换、开场白顺序、世界书过滤、脚本安全审阅、原生 Persona 双向管理与 HTML 渲染打通。

## 开源许可与致谢

- 本项目基于 [MIT License](LICENSE) 开源。
- 致谢与灵感来源：本项目在初期设计上受到社区项目 `astrbot_plugin_SillyTavern_card` (by LKarxa) 的启发，在此致谢。
