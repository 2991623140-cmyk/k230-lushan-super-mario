# 第三方项目和素材声明

## FullScreenMario

- 项目：FullScreenMario
- 来源仓库：https://github.com/beyondlimits/FullScreenMario
- 本地来源目录：`C:\Users\Honor\Desktop\超级马里奥\第三方资源包\FullScreenMario\FullScreenMario-master\`
- 来源项目许可证：MIT，原始许可证文件位于来源目录的 `LICENSE.txt`
- 本工程参考内容：原版 32 关地图结构、物件定义和部分图形资源

## 本地原始素材目录

本工程还参考了本机目录：

```text
C:\Users\Honor\Desktop\超级马里奥\超级马里奥源文件\
```

该目录是本地保存的 Super Mario 原始项目/素材文件，与下面的 FullScreenMario 目录属于两个不同来源。它们经过人工整理、地图转换、图像裁剪和 RGB565 编码后，生成了本仓库 `data/mario/` 下的运行资源。

本地 FullScreenMario 目录为：

```text
C:\Users\Honor\Desktop\超级马里奥\第三方资源包\FullScreenMario\
```

本地路径只用于记录来源，其他开发者无法直接访问。公开仓库应同时提供可访问的来源链接、版本信息和许可证说明。

本工程的 Python/MicroPython 游戏逻辑由移植者“焚寂：剑灵”维护。`data/mario` 中的 PNG、RGB565 背景块和地图 JSON 是根据第三方资料整理、转换或重新编码得到的，不应被描述为任天堂官方发布或官方授权内容。

FullScreenMario 的 MIT 许可证只覆盖其许可证适用的代码和文件，不自动授予任天堂角色、商标、音效、美术和关卡素材的版权许可。发布本仓库前，请逐项确认素材的再分发权限；无法确认的素材应从公开仓库移除，并仅保留资源转换说明或下载来源。

## 固件文件

`自定义手柄固件/` 下的 `.img`、`.img.gz` 和 `.kdimg.gz` 文件仅用于本地给 K230 烧录，已在 `.gitignore` 中排除。上传 Git 时不应提交这些大型二进制文件；如确需发布，请使用 Git LFS 或项目 Release，并同时说明固件版本、来源和校验值。
