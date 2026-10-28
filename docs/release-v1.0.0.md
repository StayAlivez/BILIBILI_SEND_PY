## BILIBILI_SEND_PY v1.0.0

首个免安装版本：内置 Python 3.11.9 运行时（含 tkinter），解压即用，**无需安装 Python**。

### 使用方法

1. 下载 `BILIBILI_SEND_PY_v1.0.0_portable_win64.zip`
2. 解压到任意目录（路径可含中文和空格）
3. 双击 `run.bat` 启动；如果没反应，改双击 `debug.bat` 查看报错

### 功能

- 图形界面一键启停，替代原来在 IDEA 里跑 Spring Boot 测试的用法
- 按权重随机 / 完全随机抽取弹幕，可避免连续两条重复
- 自动解析直播间短号，显示标题、主播、分区与开播状态
- 发送前用 `nav` 接口预检 Cookie 登录态，CSRF（`bili_jct`）自动提取
- 消息库可视化增删改，支持导入/导出 JSON
- 运行日志分色显示，同时写入 `logs/bili_send.log`
- 错误码中文化：`-101` / `-111` / `10031` / `-509` / `-400` 等会给出具体原因

### 说明

- 仅支持 Windows 10 / 11 64 位
- 内置运行时来自 Python 官方 embeddable 包（3.11.9），tkinter / Tcl / Tk 从同版本完整安装补齐
- Cookie 会明文保存在 `config.json`，请勿把该文件分享给他人
- 本工具仅供个人学习，请遵守 B 站社区规则，账号风险自负

### 源码

https://github.com/StayAlivez/BILIBILI_SEND_PY
