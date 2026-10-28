# BILIBILI_SEND · 直播弹幕助手

一个用 **Python + Tkinter** 写的 B 站直播弹幕助手：打开窗口，填好房间号和 Cookie，点「开始发送」即可。
不需要数据库、不需要构建工具，也没有任何第三方 Python 依赖。

## 下载免安装版（推荐）

不想装 Python 的话，直接下载 Releases 里的压缩包：

**https://github.com/StayAlivez/BILIBILI_SEND_PY/releases**

- 包名：`BILIBILI_SEND_PY_v1.1.2_portable_win64.zip`
- 内置 Python 3.11.9 运行时（含 tkinter），解压后双击 `run.bat` 即可，**无需安装 Python**
- 仅支持 Windows 10 / 11 64 位
- 如果双击没反应，改双击 `debug.bat`，控制台里会显示具体报错

## 界面预览

发送页（Cookie 默认打码）：

![发送页](docs/send.png)

消息库：

![消息库](docs/messages.png)

运行日志：

![运行日志](docs/log.png)

## 功能

- 图形界面一键启停，发送间隔与倒计时实时显示
- 按权重随机 / 完全随机抽取弹幕，可避免连续两条重复
- 自动解析直播间短号，显示标题、主播、分区与开播状态
- **Cookie 默认打码**，需要时勾选「显示 Cookie」查看或编辑
- 可选择是否把 Cookie 保存到 `config.json`；取消保存会立即清除已写入的 Cookie
- 发送前用 `nav` 接口预检登录态，CSRF（`bili_jct`）自动提取
- 消息库可视化增删改，支持导入 / 导出 JSON
- 运行日志分色显示，同时写入 `logs/bili_send.log`
- 错误码中文化：`-101` / `-111` / `10031` / `-509` / `-400` 等会给出具体原因
- 支持命令行模式，方便挂后台

## 运行要求

- Windows 10 / 11
- Python 3.9+（已在 3.11 与 3.14 上实测；建议 [python.org](https://www.python.org/downloads/) 安装包，自带 tkinter）
- 不需要数据库、不需要额外依赖

## 从源码运行

1. 克隆或下载本仓库。
2. 双击 `run.bat`（或执行 `python main.py`）。
3. 填房间号，点「检测直播间」确认房间号、标题与开播状态。
4. 粘贴 Cookie（必须包含 `SESSDATA` 与 `bili_jct`）。
5. 确认消息文件、发送间隔、颜色 / 位置，点 **▶ 开始发送**。
6. 想停就点 **■ 停止**；状态栏会实时显示已发送 / 成功 / 失败与倒计时。

### Cookie 怎么拿

1. 浏览器登录 [bilibili.com](https://www.bilibili.com)。
2. 按 `F12` 打开开发者工具 → 「网络 / Network」→ 刷新页面。
3. 随便点一个请求，在「请求头 / Request Headers」里找到 `Cookie:`，把整串值复制出来。
4. 回到程序点「粘贴剪贴板」。

### 录屏 / 演示时的隐私

- Cookie 框默认是密码框，只显示圆点；仍可直接输入或粘贴，**不会把值显示在屏幕上**。
- 只有勾选「显示 Cookie」才会明文显示；录完记得再取消勾选。
- 「保存 Cookie 到 config.json」默认开启。勾选时，粘贴 Cookie、输入框失焦或关闭窗口都会自动写入；录屏前建议取消勾选，程序会立即清除配置文件里已保存的 Cookie。
- 打码状态下，状态栏只显示「Cookie 可用（已打码）」，不显示 UID。
- 运行日志只记录「已粘贴 N 个键」和账号昵称，不会写入 Cookie 内容或 UID。
- 免安装包里不包含 `config.json`，解压后是干净的初始状态。

## 消息文件格式

`messages/` 下自带两份示例，两种写法都支持：

```json
["好听", "666", "可爱"]
```

```json
[
  { "msg": "好听", "weight": 3 },
  { "msg": "666", "weight": 1 }
]
```

- `weight` 越大越容易被抽到；`weight` 为 0 表示暂不参与。
- 也接受单个对象 `{"msg": "...", "weight": 1}`。
- 想完全等概率就选「完全随机」。
- 弹幕内容里的换行会被压成空格；超过 20 字会在日志里提醒（B 站普通用户上限）。

## 目录结构

```
BILIBILI_SEND_PY/
├── main.py                 # 入口：默认 GUI，--cli 走命令行
├── run.bat                 # Windows 双击启动（免安装版自动用内置 runtime）
├── debug.bat               # 同上，但保留控制台，便于看报错
├── config.json             # 配置（首次运行/关闭界面时自动生成，含 Cookie，不提交）
├── config.example.json     # 配置样例（无 Cookie）
├── messages/               # 弹幕消息列表
│   ├── weighted.json       # 带权重（默认使用）
│   └── plain.json          # 纯文本
├── bili_send/
│   ├── config.py           # 配置读写与校验
│   ├── messages.py         # 消息解析 + 权重/等概率抽取
│   ├── client.py           # B 站接口：Cookie、CSRF、房间解析、发弹幕
│   ├── runner.py           # 后台发送线程（可随时停止）
│   ├── gui.py              # Tkinter 界面
│   └── logbus.py           # 日志汇入界面
├── tools/
│   ├── build_portable.py   # 构建内置 Python 的免安装包
│   └── publish_release.py  # 创建 Release 并上传压缩包
├── tests/test_core.py      # 单元测试（不联网）
└── logs/bili_send.log      # 运行日志（自动生成）
```

## 命令行模式

想挂后台、不需要窗口时：

```bat
python main.py --cli
```

它读取同一份 `config.json`，按 Ctrl+C 停止；适合配合任务计划或开机自启。

## 测试

```bat
py -3 -m unittest discover -s tests -v
```

覆盖消息文件解析、JSON 容错、权重抽取、Cookie 解析与打码、发送表单编码、接口错误码、
配置读写与发送线程启停，全部使用假网络，不会真的发弹幕。

## 打包免安装版

需要本机有一份**带 tkinter 的 Python 3.11 完整安装**（版本与 embeddable 包一致）：

```bat
py -3 tools/build_portable.py --python-home "C:\...\pythoncore-3.11-64" --proxy http://127.0.0.1:7897
```

脚本会下载官方 embeddable 包（缓存到 `.cache/`）、补入 tkinter / Tcl / Tk，
自检通过后输出 `dist/BILIBILI_SEND_PY_v1.1.2_portable_win64.zip`。

## 注意事项

- **频率**：B 站对弹幕有冷却限制，太快会返回 `10031` / `-509`。建议间隔 ≥ 10 秒。
- **自动停止**：登录态失效（`-101` / `-111`）或连续失败 5 次时，发送线程会自动停下并弹窗提示。
- **风控**：本工具仅用于个人学习，弹幕内容和账号风险由使用者自负，请遵守 B 站社区规则。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 发送失败 `code=10031` / `-509` | 间隔太短，调大「发送间隔」 |
| 发送失败 `code=-101` | Cookie 过期，重新复制 `SESSDATA` 等整串 Cookie |
| 发送失败 `code=-111` | Cookie 里缺少 `bili_jct`，或该值与登录态不匹配 |
| 发送失败 `code=-400` | B 站对未登录请求会返回「请求错误」；检查 Cookie 是否已失效 |
| 启动时提示「Cookie 已失效」 | `nav` 接口判定未登录，重新复制 Cookie（整串） |
| 提示 Cookie 不完整 | 复制「网络」里 `Cookie` 整串，不要只复制 `SESSDATA` |
| 双击 `run.bat` 闪退 | 改双击 `debug.bat` 看报错；或重装 python.org 版并勾选 Add to PATH |
| 界面中文乱码 | 用 `run.bat` 启动（内部已 `chcp 65001`） |
| 想改默认房间/间隔 | 直接在界面改，关闭时自动写入 `config.json` |
