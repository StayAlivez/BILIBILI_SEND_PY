## BILIBILI_SEND_PY v1.1.3

修复：关闭窗口时没有保存 Cookie。

### 变更

- 绑定窗口关闭事件：点右上角 X 会保存当前设置和 Cookie
- 勾选「保存 Cookie」时，粘贴 Cookie 或输入框失焦会自动写盘
- 重新勾选「保存 Cookie」会立刻写回配置，不需要等关闭窗口
- 关闭时即使其它字段（房间号 / 间隔）不合法，也会按保存开关把 Cookie 写回
- 其余行为不变：打码输入、显示切换、日志不写 UID
- 免安装包仍内置 Python 3.11.9 + tkinter

### 使用

下载 `BILIBILI_SEND_PY_v1.1.3_portable_win64.zip`，解压后双击 `run.bat`；
如果没反应，双击 `debug.bat` 可以看到具体报错。
