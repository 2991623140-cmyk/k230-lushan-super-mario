# -*- coding: utf-8 -*-
# K230 超级马里奥入口：开机直接进入 World 1-1
try:
    import sys
    # CanMV IDE 重复运行脚本时可能保留模块缓存，强制加载板端最新版本。
    if 'super_mario' in sys.modules:
        del sys.modules['super_mario']
    import super_mario
    super_mario.main(auto_start=False)
except KeyboardInterrupt:
    pass
except BaseException as e:
    try:
        with open('/sdcard/mario_error.txt', 'w') as f:
            f.write('main: %s: %s\n' % (type(e).__name__, e))
    except BaseException:
        pass
    raise
