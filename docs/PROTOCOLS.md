# 协议层

QSICK 把「协议」从功能里拆出来，放在项目根的 `protocols/` 下。每个目录就是一份
实现，目录名即协议名。

| 目录 | 角色 | 说明 |
|---|---|---|
| `onebotv11/` | 聊天 | 纯 OneBot v11。表情只能表达基础黄脸：`[CQ:face,id=N]` 只有一个数字，说不出「是否动态」「什么文件类型」。 |
| `onebotv11-EXTemotions/` | 聊天 | OneBot v11 承载消息，**表情改用 EXTemotions 对象**（类型、动态性、文件格式、贴纸三元组全部显式声明）。 |
| `maxStore/` | 存储 | 不是聊天协议，**不可被选作聊天协议**。保留收到的全部原始信息，包括未知字段与未识别元素。 |

## 注册表

`enabled_protocol.json` 是可选项清单：网络配置的协议下拉框只列里面 `chat` 数组
的条目。`store` 字段固定指向 maxStore。

清单与磁盘不一致时**明确报错**，不静默跳过——少一个协议比多一个错更容易查。

## 三者怎么协作

```
QQ 宿主事件
   │  先完整落盘（原始 payload + 解析后字段）
   ▼
protocols/maxStore  ── 该实例自己的库：data/instances/<实例>/messages.db
   │
   │  再按该实例每条网络配置选定的协议编码
   ▼
protocols/processor.py  ── 按协议编码 ──▶  OneBot v11 段数组 ──▶ 各个网络配置
   │
   │  同时把收到的段解码回内部部件
   ▼
浏览器输入框 / 插件 / 动作
```

`processor.py` 是唯一的跨界面：消息/指令进出都经过它，因此加一个协议只需要加
一个目录，不需要在每个功能里加分支。

**处理器是按实例的**：每个实例有自己的处理器缓存（同一实例的多条配置若选同一协议
则共用一份），因此 C0 与 C1 的协议选择互不影响，各自的网络配置也各收各的消息。

## EXTemotions

表情对象与收发语义遵循 EXTemotions 规范（v1.0）。在消息里以
`[CQ:ext_emotion,json=<base64url>]` 承载（规范附录 B 的兜底形态），这样纯 v11
消费方读到不认识的段会跳过，而不会把整条消息判废。

六个强制字段：`schema`、`version`、`type`、`uid`、`moveable`、`fileType`。
核心区分：`moveable` 说「会不会动」，`fileType` 说「实际是什么字节」。两者不能
互相顶替——把只有静态首帧的 PNG 说成 APNG 能让不变量过关，但那是谎报格式。

## 表情素材

表情图有**两个来源**，运行时的查找顺序是「客户端优先、内置库兜底」
（`src/qsick/webui/face_assets.py`）：

```python
def face_image_path(headless_root, face_id):
    live = client_face_path(headless_root, face_id)   # ① 实例自己的 QQ 数据目录
    if live is not None:
        return live
    return verified_face_path(face_id)                # ② assets/qq-faces 内置库
```

**① QQ 客户端资源**——登录后 QQ 自己写进实例目录，不需要任何额外准备：

- `<实例>/data/qq/qq/<账号>/nt_qq/nt_data/Emoji/BaseEmojiSyastems/EmojiSystermResource/<id>/png/<id>.png`
- `<实例>/data/qq/qq/nt_qq/global/nt_data/Emoji/emoji-resource/sysface_res/{apng,static}/s<id>.png`

**② 内置库 `assets/qq-faces/`**——**图片不随仓库分发**（约 58 MB 第三方二进制，
进了 Git 就几乎无法从历史里清掉，见 `.gitignore`）。只有 `index.json`、
`face_config.json`、`face_config_qq.json` 随仓库走，它们决定资源该怎么组织。

### 没有内置库时会缺什么

新克隆的仓库里没有图片，表情栏会**降级但不会崩**——缺的图显示占位符，收发消息不受
影响。实测降级范围（以 525 条索引为基准）：

| 类别 | 数量 | 客户端资源能否提供 | 缺失后果 |
| --- | ---: | --- | --- |
| 数字表情（`kind: face` / `super`） | 360 | 能提供 **341 个（95%）** | 约 19 个显示占位符 |
| emoji 字符表情（`kind: emoji`） | 165 | **一个都没有** | 全部显示占位符 |

也就是：**数字表情基本完整，emoji 全缺**。原因是 emoji 在客户端资源树里不以 id
命名、也不在那个目录下，内置库是它们的唯一来源。

### 补回来

```bash
# 从本机已登录的 QQ 客户端抓取（只复制客户端自己下载的原图，不联网、不推测）
python tools/import_from_client.py --dry-run   # 先看能补多少
python tools/import_from_client.py             # 实际复制

# 或者从表情语料完整重建（需要一份 qq-emotions 语料目录，不随仓库分发）
python tools/import_emotions.py --source <语料目录> --dry-run
python tools/import_emotions.py --source <语料目录>
```

`import_from_client.py` 能补回 341 个数字表情，但补不了 emoji——那 165 个只能靠
`import_emotions.py` 从语料重建。

两个脚本都有一道闸门：**索引里没有登记的 id 一律不导入**。库里多一张来路不明的图，
会让聊天里画出一张错的脸，比不显示更糟。

`import_emotions.py` 的产出：`<id>.png`（黄脸/超级表情预览）、`<字符>.png`（emoji）、
`super/<id>/`（Lottie 等动效资源）、`index.json`（导入索引）、
`face_config.json`（含 QQ 的 `AniSticker*` 三元组）。

文件格式**按魔数嗅探**，不信扩展名：语料里 `apng/419.png` 的扩展名是 `.png`，
内容却是 APNG——按扩展名判会把动态表情全判成静态。
