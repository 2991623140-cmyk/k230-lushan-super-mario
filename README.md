# K230 Super Mario 移植工程

这是一个面向立创·庐山派 K230-CanMV 开发板的 Super Mario 移植工程。工程保留了电脑端验证入口，并将板端程序和资源整理成可以直接复制到 K230 文件系统的目录结构。

- 中文项目名：`k230庐山派-超级马里奥移植`
- GitHub 仓库：https://github.com/2991623140-cmyk/k230-lushan-super-mario
- 发布包下载：https://github.com/2991623140-cmyk/k230-lushan-super-mario/releases
- 当前移植者：焚寂：剑灵

## 项目记录

| 项目 | 内容 |
|---|---|
| 当前移植者 | 焚寂：剑灵 |
| 移植完成版本标记 | 2026.19.15 |
| 目标硬件 | 立创·庐山派 K230-CanMV（`k230_canmv_lckfb`） |
| 运行环境 | CanMV MicroPython，当前测试固件为 CanMV v1.6 |
| 显示分辨率 | 800 x 480 |
| 主要语言 | MicroPython / Python |
| 工程状态 | 可在电脑端验证，并可复制到 K230 独立运行 |

> “2026.19.15”是项目记录中的版本日期标记，本文按原记录保留。后续发布 Git 标签时建议使用合法日期格式，例如 `v2026.09.15`。

## 目录结构

```text
入k230/
├── README.md                 # 本说明
├── THIRD_PARTY_NOTICES.md    # 第三方项目、素材和固件声明
├── .gitignore                # Git 排除规则
├── 放置路径说明.md             # 最简复制路径说明
├── sdcard/
│   ├── main.py               # K230 开机入口
│   └── super_mario.py        # 游戏主程序和全部游戏逻辑
└── data/
    └── mario/
        ├── manifest.json     # 关卡、背景块和资源索引
        ├── maps/             # 63 个关卡/子地图 JSON
        ├── bg/               # 973 个 RGB565 压缩背景块
        ├── sprites/          # 105 个角色、敌人和道具图像
        └── masks/            # 105 个透明遮罩图像
```

本目录还可能存在本地使用的 `自定义手柄固件/` 文件夹。固件镜像不属于游戏运行时文件，且体积很大，已由 `.gitignore` 排除，不应随普通 Git 提交上传。第三方来源和许可证详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

电脑端工程位于上一级目录：

```text
C:\Users\Honor\Desktop\超级马里奥\k230我的工程\
```

电脑端入口是 `游戏.py`，它会提取 `sdcard/super_mario.py` 中的公共游戏逻辑，并使用 pygame 提供显示、键盘和文件加载适配层。因此板端和电脑端应尽量共用同一份游戏逻辑，不要分别修改两套物理或碰撞代码。

## K230 文件对应关系

复制时不要把 `入k230` 这一层目录复制到开发板。对应关系如下：

| 本地路径 | K230 目标路径 |
|---|---|
| `入k230/sdcard/main.py` | `/sdcard/main.py` |
| `入k230/sdcard/super_mario.py` | `/sdcard/super_mario.py` |
| `入k230/data/mario/` | `/data/mario/` |

最终至少要确认以下文件存在：

```text
/sdcard/main.py
/sdcard/super_mario.py
/data/mario/manifest.json
/data/mario/maps/level_1.json
/data/mario/bg/level1_00.rgb565z
```

禁止出现以下错误路径：

```text
/data/data/mario/
/data/mario/mario/
/sdcard/sdcard/main.py
/入k230/data/mario/
```

## 推荐部署顺序

1. 打开 CanMV IDE K230，连接目标板并确认串口终端可以看到 REPL。
2. 将整个 `入k230/data/mario` 目录复制到开发板 `/data/mario`。
3. 确认资源复制完成后，再复制 `入k230/sdcard/super_mario.py` 到 `/sdcard/`。
4. 最后复制 `入k230/sdcard/main.py` 到 `/sdcard/`。
5. 在 IDE 中执行软复位，或按开发板 RESET 键。
6. 观察串口终端。正常启动时不会出现 `mario_error.txt`，游戏会进入开始界面。

重新烧录固件前必须备份 `/sdcard` 和 `/data/mario`。固件镜像可能会覆盖 TF 卡或主分区中的文件。

## 游戏运行方式

`main.py` 会清理旧的 `super_mario` 模块后重新导入，避免 CanMV IDE 重复运行时继续使用旧代码。板端启动流程为：

```text
main.py
  -> import super_mario
  -> 初始化 Display / MediaManager
  -> 初始化 FT5316 触摸
  -> 加载精灵、遮罩和关卡清单
  -> 显示标题界面
  -> 选择关卡并加载当前关卡背景
  -> 进入游戏循环
```

### 控制方式

板端触摸按钮和 USB 手柄/键盘适配由 `super_mario.py` 统一处理。当前逻辑支持同时保持方向和跳跃等多个按键状态。

电脑端默认键位：

| 功能 | 键位 |
|---|---|
| 左移 | 方向左 / A |
| 右移 | 方向右 / D |
| 下蹲、进管道 | 方向下 / S |
| 跳跃 | Z / K / 上 / 空格 |
| 奔跑、发射火球 | X / J / 左 Shift |
| 暂停 | P / Enter |

暂停菜单包含继续游戏、重新开始和选择关卡。进入管道时需要保持下方向，横向入口根据关卡数据判断有效方向。

## 关卡和资源格式

### 第三方素材来源

本工程使用的素材和关卡参考资料来自以下两个本机目录：

```text
C:\Users\Honor\Desktop\超级马里奥\超级马里奥源文件\
C:\Users\Honor\Desktop\超级马里奥\第三方资源包\FullScreenMario\
```

其中，`超级马里奥源文件` 是本地保存的原始项目/素材来源，`FullScreenMario` 是用于参考完整关卡、物件定义和网页端实现的第三方开源项目。两者均不是 K230 运行时目录，K230 实际使用的是本仓库内经过整理和转换后的 `data/mario` 文件。

FullScreenMario 的本地目录为：

```text
C:\Users\Honor\Desktop\超级马里奥\第三方资源包\FullScreenMario\FullScreenMario-master\
```

来源项目资料：

| 项目 | 内容 |
|---|---|
| 项目名称 | FullScreenMario |
| 来源仓库 | `https://github.com/beyondlimits/FullScreenMario` |
| 来源说明 | HTML5 版 Super Mario Bros remake，包含原版 32 个主关卡及附加地图/编辑器 |
| 来源仓库许可证 | MIT（以来源仓库中的 `LICENSE.txt` 为准） |
| 本地说明 | `第三方资源包/FullScreenMario/说明.md` |
| 本地完整移植说明 | `第三方资源包/FullScreenMario/MGL5.3_K230_完整32关实现说明.md` |

本 K230 工程不是把 FullScreenMario 的 HTML/JavaScript 程序直接搬到板端，而是参考其关卡定义和资源，在 Python/MicroPython 中重新组织为：

```text
FullScreenMario Source/settings/maps/  ->  data/mario/maps/*.json
FullScreenMario 的图形资源             ->  data/mario/sprites/*.png
透明区域定义                           ->  data/mario/masks/*.png
背景图切片并转换为 RGB565               ->  data/mario/bg/*.rgb565z
```

资源转换和地图整理属于本移植工程的生成工作；如果重新生成资源，应保留来源仓库、转换脚本和转换日期，确保其他人可以追溯 `data/mario` 中文件的来源。

`manifest.json` 保存关卡编号、显示名称、背景块数量和背景块宽度。`maps/level_N.json` 保存对应关卡的碰撞体、砖块、箱子、金币、敌人、传送管道、旗杆和出生点。

背景文件使用自定义的 `.rgb565z` 格式：

```text
文件头：little-endian uint16 width
        little-endian uint16 height
数据：  zlib 压缩后的 RGB565 像素数据
```

板端通过 `image.Image(width, height, image.RGB565)` 建立图像，再使用 `DeflateIO` 分段解压。背景块已经是显示格式，不应在游戏运行时从 PNG 重新转换。PNG 主要用于精灵和遮罩资源。

## 背景加载和缓存策略

当前策略是“只加载当前关卡，加载结束后再开始游戏”：

- 不再后台解压相邻关卡，不让后台任务抢占游戏帧时间。
- 进入关卡前完整读取本关所有背景块。
- 加载过程中显示当前关卡和 `N/总数` 进度。
- 当前关卡加载完成后才将状态切换为 `PLAYING`。
- 死亡重新开始同一关时直接复用缓存，不重复读取背景文件。
- 最近使用的关卡最多保留 5 关。
- 超过 5 关，或总背景块超过安全上限时，释放最旧关卡的图像并执行垃圾回收。

相关配置在 `super_mario.py` 顶部：

```python
BG_KEEP_LEVELS = 5
BG_CACHE_MAX_CHUNKS = 50
BG_LOAD_BYTES = 32768
```

`BG_KEEP_LEVELS` 是关卡数量上限，`BG_CACHE_MAX_CHUNKS` 是内存安全阈值。由于不同关卡背景块数量差异较大，实际缓存可能少于 5 关，但当前关卡优先级最高。

## 性能说明

当前显示流程使用一个 800 x 480 RGB565 画布，静态背景由已经转换好的背景块组成，动态角色、敌人、道具和 HUD 每帧绘制。角色透明区域使用遮罩矩形绘制，以避免 `image.draw_image(..., mask=bitmap)` 在当前固件上产生黑色背景框。

KPU 是 AI 推理单元，VPU 主要用于视频处理；当前 CanMV Python API 没有将 zlib 背景解压直接交给 KPU/VPU 的接口。因此资源加载使用大块连续读取和预先转换 RGB565 的方式加速。

K230 标准板的 1GB DDR 是板端物理内存，电脑上的内存不能直接提供给 K230。MicroPython 图片对象、显示缓冲、精灵遮罩、地图对象和解压临时缓冲还会共同消耗运行堆，所以不能把所有 973 个背景块全部解压并常驻内存。

## 电脑端验证

在电脑上运行：

```powershell
cd "C:\Users\Honor\Desktop\超级马里奥\k230我的工程"
python 游戏.py
```

快速回归测试：

```powershell
python test_mario_logic.py
```

测试覆盖资源加载、FT5316 触摸数据解析、四个基础关卡绘制、组合按键、碰撞和缓存逻辑。电脑端测试通过不等于替代 K230 真机测试，板端仍需检查实际内存、显示和触摸响应。

## 串口和错误排查

CanMV IDE K230 串口终端常见信息：

```text
MPY: soft reboot
```

表示 MicroPython 已软复位，通常不是错误。

程序异常时查看：

```text
/sdcard/mario_error.txt
/sdcard/mario_boot_ok.txt
/sdcard/mario_perf.txt
```

常见问题：

| 现象 | 检查方向 |
|---|---|
| `Image is compressed!` | 不要直接绘制压缩 PNG；确认使用 RGB565 或遮罩矩形绘制 |
| `module object has no attribute path` | 板端不要使用桌面 Python 的 `os.path`，资源路径使用 `/data/mario` |
| 黑屏 | 检查 `main.py`、资源根路径、固件板型是否匹配，并查看 `mario_error.txt` |
| 进入关卡等待较久 | 当前关卡正在完整预加载，等待进度到总数后才开始游戏 |
| 死亡后仍然重新加载 | 确认复制的是最新 `super_mario.py`，并确认没有手动删除缓存或重启板卡 |
| 触摸无响应 | 检查 FT5316 连接、I2C 总线和 CanMV 固件；USB 键盘可用于排除游戏逻辑问题 |

## Git 开源建议

建议提交以下内容：

```text
README.md
放置路径说明.md
sdcard/main.py
sdcard/super_mario.py
data/mario/manifest.json
data/mario/maps/
data/mario/bg/
data/mario/sprites/
data/mario/masks/
```

建议不要提交运行生成文件：

```text
sdcard/mario_error.txt
sdcard/mario_boot_ok.txt
sdcard/mario_perf.txt
sdcard/__pycache__/
```

可以在仓库根目录增加 `.gitignore`：

```gitignore
__pycache__/
*.pyc
mario_error.txt
mario_boot_ok.txt
mario_perf.txt
```

开源发布时需要单独说明：FullScreenMario 仓库的代码许可证是 MIT，但 MIT 许可不等于任天堂原始角色、商标、音效和美术素材的版权许可。游戏代码由本移植者维护，不能声称任天堂素材为原创，也不能使用“官方授权”字样。发布仓库时应：

1. 保留 FullScreenMario 的来源链接、作者信息和 `LICENSE.txt` 内容。
2. 在 README 中明确 `data/mario` 是由第三方项目参考/转换得到的板端资源。
3. 根据实际素材文件的版权和许可证决定是否直接分发 PNG、音频和转换后的 RGB565 文件。
4. 如果无法确认某个素材的再分发权限，只发布代码、地图格式和资源转换脚本，不直接上传该素材。
5. 明确项目仅用于学习、研究和嵌入式移植测试，不代表任天堂或 FullScreenMario 原作者。

## 后续维护原则

1. 优先修改 `sdcard/super_mario.py`，电脑端会自动提取同一份公共逻辑。
2. 修改资源路径时必须同步检查 `RESOURCE_DIR` 和 `manifest.json`。
3. 修改缓存策略时先保留当前关卡完整加载和死亡重开复用，再考虑增加缓存数量。
4. 每次提交前运行 `python -m py_compile` 和 `python test_mario_logic.py`。
5. 真机更新只替换必要文件，资源未变化时不要重复复制整个 `data/mario`。
6. 发生黑屏或卡顿时先读取串口和 `mario_error.txt`，不要直接删除缓存或重新烧录固件。

## 快速上传 Git

在 PowerShell 中执行以下命令，可以把当前目录作为一个独立仓库上传。将地址替换为你自己的 Git 仓库地址：

```powershell
cd "C:\Users\Honor\Desktop\超级马里奥\k230我的工程\入k230"
git init
git add .
git status
git commit -m "移植完成：K230 Super Mario 2026.19.15"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库.git
git push -u origin main
```

执行 `git status` 时，不应看到 `自定义手柄固件/*.img`、`*.img.gz`、`*.kdimg.gz` 或 `__pycache__`。如果使用 GitHub 网页上传、网盘同步或直接压缩整个目录，`.gitignore` 不会替你删除文件；此时请手动跳过 `自定义手柄固件` 中的大型镜像和 `sdcard/__pycache__`。
