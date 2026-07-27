# -*- coding: utf-8 -*-
"""公益站 & 账号管理 GUI 包。

模块划分（见 docs/OPTIMIZATION.md §三）：
- core    ：纯逻辑层（不依赖 Qt）——行归一化 / auth 矫正 / 任务参数装配 / 快照 / 状态缓存 / 脱敏日志
- theme   ：设计令牌（深浅两套）+ QSS 生成 + 偏好持久化
- workers ：TaskRunner（线程池跑 providers 调用）+ BrowserWorker（仅交互式捕获/检测）
- widgets ：列表项 / 徽标 / 统计块 / 日志面板等纯展示组件
- dialogs ：新增站点等对话框
- app     ：主窗口装配与入口 main()
"""
