# Windows 任务状态监控工具

使用 Python 读取本地任务状态和结构化日志，根据独立会话的状态变化生成提醒，并提供桌面悬浮球。核心设计包括状态机、去重、超时阈值、只读 SQLite 访问与本地通知记录。

## 建议阅读

- 状态机和去重：`src/watchdog_core.py`。
- 安全监控入口：`tools/safe_monitor.py`。
- 桌面悬浮球：`tools/floating_ball_watchdog.py`。
- 离线回归：`tests/test_watchdog.py`、`fixtures/acceptance_events.jsonl`。

## 行为边界

工具用于状态观察与提醒，不自动批准、点击、重试或控制被监控任务。状态来源不完整时应显示未知，不能从进程存活推断整个任务完成。

## 验证与使用

原项目记录包含 10 项核心自动化测试及 Windows 本地接入观察；本展示副本发布时未重新进行长时间实机观察。离线测试可使用 `python -m unittest discover -s tests -v`。

真实监控依赖本地应用的日志结构与权限。安全入口为 `python tools/safe_monitor.py start`，状态查询为 `python tools/safe_monitor.py status`。历史兼容启动脚本和个人观察日志未包含在此展示仓库中。
