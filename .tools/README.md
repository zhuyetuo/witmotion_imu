# .tools —— 离线安装包放这里

`setup_windows.sh`（`setup_windows.bat` 会调它）装 Miniconda、ffmpeg 时，**先在这个目录里找安装包，
找不到才去网上下**。所以没网 / 网很慢的电脑，把下面两个文件拷进这个目录再跑安装就行。

| 放什么 | 文件名（按通配认，版本号对不上也行） | 从哪下 |
|---|---|---|
| Miniconda（Python 3.13） | `Miniconda3-py313_26.7.1-1-Windows-x86_64.exe`（`Miniconda3-*.exe` 都认） | https://repo.anaconda.com/miniconda/Miniconda3-py313_26.7.1-1-Windows-x86_64.exe |
| ffmpeg | `ffmpeg*.zip`，比如 `ffmpeg-8.1.1-essentials_build.zip` | https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip |

放好后在仓库目录里：

```bash
./setup_windows.sh
```

说明：

- 直接丢在 `.tools/` 下就行，也可以放 `.tools/cache/`，两个地方都会找。
- 装完会多出 `.tools/miniconda3/`、`.tools/ffmpeg/`、`.tools/cache/`，这些都**不进 git**
  （一台机器一份；Miniconda 一百多 MB、ffmpeg 一百多 MB，进了仓库以后每台 clone 都要拖这么大）。
  只有这个 README 进 git，所以新 clone 下来目录就在。
- 建议在 NAS 上留一份这两个安装包，新电脑从 NAS 拷过来最快。
- Python 包（`requirements.txt`）走清华镜像在线装；那台电脑完全没网的话另说。
