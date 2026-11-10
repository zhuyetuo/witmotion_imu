# .tools —— 离线安装包放这里

`setup_windows.sh`（`setup_windows.bat` 会调它）装 Miniconda、ffmpeg 时，**先在这个目录里找安装包，
找不到才去网上下**。所以没网 / 网很慢的电脑，把下面两个文件拷进这个目录再跑安装就行。

| 放什么 | 文件名（按通配认，版本号对不上也行） | 从哪下 |
|---|---|---|
| Miniconda（**必须是 Python 3.13 这一版**） | `Miniconda3-py313_26.7.1-1-Windows-x86_64.exe`，文件名一个字都别改 | https://repo.anaconda.com/miniconda/Miniconda3-py313_26.7.1-1-Windows-x86_64.exe |
| ffmpeg | `ffmpeg*.zip`，比如 `ffmpeg-8.1.1-essentials_build.zip` | https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip |

放好后在仓库目录里：

```bash
./setup_windows.sh
```

说明：

- **Miniconda 别用 `Miniconda3-latest-...`**：latest 现在带的是 Python 3.14，采集用的蓝牙库 bleak 装不上。
  脚本只在下载失败、手边又只有别的版本时才退而用它，之后还得联网把 Python 换回 3.13。
- 安装包大小对一下：Miniconda 九十多 MB、ffmpeg 一百多 MB。小于 60MB 的 Miniconda 是没下完的半截包，
  脚本会认出来接着下。
- 静默安装 Miniconda 要 1～3 分钟，屏幕上停在「静默安装到 ...」不动是正常的，每 30 秒会打一行「还在装」；
  最多等 5 分钟。

- 直接丢在 `.tools/` 下就行，也可以放 `.tools/cache/`，两个地方都会找。
- 装完会多出 `.tools/miniconda3/`、`.tools/ffmpeg/`、`.tools/cache/`，这些都**不进 git**
  （一台机器一份；Miniconda 一百多 MB、ffmpeg 一百多 MB，进了仓库以后每台 clone 都要拖这么大）。
  只有这个 README 进 git，所以新 clone 下来目录就在。
- 建议在 NAS 上留一份这两个安装包，新电脑从 NAS 拷过来最快。
- Python 包（`requirements.txt`）走清华镜像在线装；那台电脑完全没网的话另说。
