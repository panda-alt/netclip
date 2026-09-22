"""netclip - 两台 Windows 电脑之间共享鼠标/键盘并同步剪贴板。

模块结构::

    netclip/
        config.py           TOML 配置加载与校验
        log.py              日志
        protocol.py         TCP 帧编解码 + 消息类型
        layout.py           屏幕摆放关系 / 边缘检测 / 坐标映射（纯逻辑，可单测）
        core/
            router.py       输入路由状态机 LOCAL <-> REMOTE
            watchdog.py     心跳、断线回拉、松键
        win/
            winapi.py       ctypes Win32 绑定
            hooks.py        低级鼠标/键盘钩子
            inject.py       SendInput 注入
            clipboard.py    剪贴板读写（避开占用冲突）
            clip_formats.py 剪贴板格式的采集/还原（全格式转发）
            hdrop.py        CF_HDROP 构造与解析
            msgwin.py       隐藏消息窗口（WM_CLIPBOARDUPDATE）
        net/
            channel.py      单条 TCP 通道
            manager.py      三通道（input/clip/file）管理
        files/
            transfer.py     文件流式收发
            staging.py      接收端暂存目录与 TTL 清理
    """

__version__ = "0.1.0"
__all__ = ["__version__"]
