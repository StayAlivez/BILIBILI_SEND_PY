## BILIBILI_SEND_PY v1.1.0

### 新增

- **Cookie 默认打码**：界面上只显示 `SESSDATA=••••••••` 这样的文本，录屏 / 演示时不会把 Cookie 值拍进去
- 新增「显示 Cookie」开关：需要查看或编辑时手动勾选，取消后立即重新打码
- 新增「保存 Cookie 到 config.json」开关：取消勾选会立刻清除已写入的 Cookie，之后启动也不会自动填入
- 打码状态下，状态栏只提示「Cookie 可用（已打码）」，不再显示 UID
- 推送脚本升级为一键发布：构建免安装包 → 推送代码与 tag → 创建 Release → 上传压缩包

### 调整

- README 精简，去掉旧版本对比，补充隐私与录屏说明
- 免安装包仍内置 Python 3.11.9 + tkinter，解压即用

### 使用

下载 `BILIBILI_SEND_PY_v1.1.0_portable_win64.zip`，解压后双击 `run.bat`；
如果没反应，双击 `debug.bat` 可以看到具体报错。
