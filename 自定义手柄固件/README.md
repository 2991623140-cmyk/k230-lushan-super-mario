# K230 USB 手柄支持固件

## 当前状态

本目录中的镜像是在官方 CanMV K230 源码基础上编译的自定义固件，目标板为嘉立创 LCKFB K230（`k230_canmv_lckfb`）。它保留原版 CanMV、MicroPython、摄像头和显示功能，并额外加入了 USB HID 游戏手柄识别以及 MicroPython 接口 `usb.Gamepad`。当前镜像还针对 `Microntek USB Joystick`（VID `0079`、PID `0006`）扩展到 12 个按钮，包含 Select/Start。

编译所依据的版本：

- CanMV commit: `8f620e570e90363678bd2fb9fdb22f4e16ba8fbf`
- RT-Smart commit: `8be926875889b1059f4289b84ed1bd519cdd2889`
- 目标配置：`k230_canmv_lckfb_defconfig`
- 生成时间：2026-09-15（12 按钮修正版）

## 刷写文件

正常整机刷写请使用：

`CanMV_K230_LCKFB_micropython_local_nncase_v2.11.0.img`

同名 `.img.gz` 是压缩传输版本，只有刷写工具明确要求压缩镜像时才使用；`_ota.kdimg.gz` 是 OTA 升级包，不用于首次整机刷写。

刷写整机镜像通常会重建系统分区，`/sdcard` 中原有文件可能被清除。刷写前必须确认已经保留本工程的以下文件：

- `入k230/sdcard/main.py`
- `入k230/sdcard/super_mario.py`
- `入k230/data/mario/` 整个资源目录

## CanMV IDE K230 刷写流程

1. 关闭正在运行的脚本和串口终端，退出可能占用 COM6 的其他程序。
2. 打开 `CanMV IDE K230`，进入固件烧录/升级界面。
3. 选择本目录的未压缩 `.img` 文件。
4. 选择 K230 对应的串口或设备。串口终端当前使用的是 COM6，但刷写模式下端口号可能临时变化，以 IDE 实际显示为准。
5. 按 IDE 提示让开发板进入下载模式：按住 BOOT，再按一下 RESET，随后松开 BOOT。不同批次板卡的按键标号可能相反，以板上丝印为准。
6. 开始烧录，等待校验和进度条完成。烧录过程中不要拔 USB、不要关闭 IDE。
7. 烧录完成后让板子自动复位；如果 IDE 提示手动复位，按一下 RESET。

如果 IDE 不接受 `.img`，不要改用 OTA 包；请在 IDE 的设备/固件升级页面选择“整机镜像”模式，或使用嘉立创 K230 官方烧录工具导入同一个 `.img` 文件。

## 刷写后恢复游戏文件

整机镜像只包含固件，不包含本项目的马里奥资源。重新连接 CanMV IDE 后，将工程文件传到以下固定路径：

```text
/sdcard/main.py          <- 入k230/sdcard/main.py
/sdcard/super_mario.py   <- 入k230/sdcard/super_mario.py
/data/mario/             <- 入k230/data/mario/ 下的全部文件
```

传输完成后复位开发板。启动脚本应从 `/sdcard/main.py` 进入游戏。不要把 `data/mario` 放到 `/sdcard/data/mario`，当前程序使用的是绝对路径 `/data/mario`。

## 验证 USB 手柄接口

先不要启动游戏，在 IDE 串行终端逐行执行：

```python
import usb
print(hasattr(usb, 'Gamepad'))
pad = usb.Gamepad(timeout_ms=20, auto_reconnect=True)
print(pad.info())
```

预期第一行是 `True`，`info()` 中应能看到 `kind: 'gamepad'` 或类似的游戏手柄设备信息。然后执行：

```python
for i in range(100):
    f = pad.read(timeout_ms=50)
    if f and f.get('count', 0):
        print(f)
```

按下方向键、跳跃键和功能键时，应看到 `events` 元组，格式为 `(type, code, value)`。常用事件类型为 `1`（按键）和 `3`（轴/方向帽）。按住方向键再按跳跃键，单帧中可以同时出现多个按键状态，这正是本固件相对于原版轮询键盘支持的改进。

如果 `hasattr(usb, 'Gamepad')` 为 `False`，说明刷写的仍是官方固件而不是本目录镜像。若接口存在但 `info()` 返回未连接，检查 USB 转接头是否工作在 Host 模式，并确认手柄接收器已经供电。

## 游戏中的按键映射

当前 `super_mario.py` 会优先使用 `usb.Gamepad`，并保留键盘回退。默认映射如下：

- 左/右：游戏手柄方向键或 X 轴
- 跳跃：手柄按钮事件中的 A/确认类按键
- 动作/冲刺：手柄按钮事件中的其他动作键
- 开始/确认：Start 或确认类按键

不同 USB 转接器可能给按钮分配不同的 HID 编号。若串口测试能读到事件但游戏按键不对应，只需根据上面的打印结果调整 `super_mario.py` 中的 `LEFT_KEYS`、`RIGHT_KEYS`、`JUMP_KEYS`、`ACTION_KEYS`、`START_KEYS`，不需要重新编译固件。

## 回滚

若需要恢复官方固件，使用之前下载的官方 CanMV K230 LCKFB `.img` 重新整机刷写，然后重新传回本工程 Python 文件和资源。自定义镜像不会修改 TF 卡上的工程源文件，但整机刷写可能清空板上存储，因此工程目录中的 `入k230` 始终是主备份。

## SHA-256 校验

在 PowerShell 中执行：

```powershell
Get-FileHash .\\CanMV_K230_LCKFB_micropython_local_nncase_v2.11.0.img -Algorithm SHA256
```

本次构建的哈希记录如下：

```text
CanMV_K230_LCKFB_micropython_local_nncase_v2.11.0.img
DA27A73C8AF424479BDAD3FA3B31741CA0EB559435CB535D98BEB7D80D9D07D5

CanMV_K230_LCKFB_micropython_local_nncase_v2.11.0.img.gz
485A833ACA2CA77A28DBBB7EDD8BA0C321CAE363778F14E126EB24F3317B730F

CanMV_K230_LCKFB_micropython_local_nncase_v2.11.0_ota.kdimg.gz
6F2622D3D43A707ACDCC9270CFB5AC542242F1A547E55C0B84DD3AAEE30D1D43
```

压缩镜像和 OTA 包也已放在本目录，但不作为普通整机刷写文件。
