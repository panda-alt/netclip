"""netclip 单元测试包。

约定：
  * `test_layout.py` / `test_protocol.py` / `test_config.py` / `test_toml_lite.py`
    是**纯逻辑**测试，不依赖 Windows，任何平台都能跑；
  * `test_win_clipboard.py` 需要真实 Windows 剪贴板，会自动跳过非 Windows；
  * 会动鼠标/注入输入的测试**默认不写**，避免干扰正常使用（见 README 的测试章节）。
"""
