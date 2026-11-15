# 回溯 RestorePoint · Qt 桌面应用

使用 PySide6 / Qt 6 构建，Linux 与 Windows 共用同一套桌面界面。
原有 `snapshot.py`、`portable.py` 和 PowerShell 工具继续保留，可独立使用。
GUI 通过后台任务调用 Linux 引擎，远程连接使用内置的 Paramiko SSH/SFTP，无需 WSL，
不依赖 PowerShell 执行备份。Windows 本机系统备份不在本项目范围内，Windows 客户端
用于管理 Linux 测试机并保存其备份。

## 界面功能

- 概览：实际版本数、目标磁盘可用空间、最近任务状态，环境检查与创建备份。
- 备份版本：仓库扫描、搜索、详情、SHA-256 校验、取回已有远端导出版本。
- 恢复：用户 / 系统范围选择，远程上传、解包、预览，预览成功后执行恢复。
- 连接与存储：多机器配置、SSH 密钥或密码认证、首次主机指纹确认、组件部署、
  配置导入导出，并可导入原 `remote.config.json`。
- 任务日志：后台任务、传输进度、中断请求、持久化历史、日志查看及导出。
- 使用帮助：在界面内说明两种模式的操作顺序、系统恢复条件和当前支持范围。

## 从源码运行

建议使用 Python 3.10+ 和虚拟环境。安装 Qt/SSH 依赖后运行：

```bash
python -m venv .venv
# Linux: .venv/bin/python；Windows: .venv\Scripts\python.exe
python -m pip install -r requirements-gui.txt
python app.py
```

上例中的 `python` 应使用已激活虚拟环境的 Python，或直接替换为虚拟环境解释器路径。
已打包的程序不需要用户安装 Python / Qt。Linux 系统引擎仍需 rsync、GNU tar、gzip、
sudo、findmnt、dpkg-query、apt-mark；因此当前面向 Debian/Ubuntu 来源。

## 首次使用

1. 在「连接与存储」选择模式并填写仓库 / 备份包目录，保存配置。
2. 远程模式填写 SSH 主机或 `~/.ssh/config` 中的别名。用户名与私钥可留空沿用 SSH
   配置；端口默认 22。GUI 支持密钥、SSH agent 和密码，ProxyJump 暂需改用 ProxyCommand。
3. 检查 SSH 连接，核对首次主机指纹，然后点击「部署 / 更新测试机组件」。
   程序将 snapshot.py、portable.py、gui_bridge.py 安装到配置的专用 root 管理目录。
   默认位置 `/usr/local/lib/test-backup/portable.py`。
4. 环境检查后创建备份。Linux 本地保存硬链接快照；远程保存独立完整包到开发机。
5. 从「备份版本」选中版本，查看详情或进入恢复。

密码在 GUI 弹窗内输入，经本地管道或关闭回显的 SSH 会话交给 sudo；密码不写入配置、
任务历史或日志。主机密钥会与 OpenSSH known_hosts 和应用自己的 known_hosts 核对，
已有主机密钥改变时拒绝连接。应用不自动修改 sudoers，不要求开放 SSH root 登录。

Linux 仓库通常只允许 root 读取，刷新本地版本时也可能需要 sudo 认证。
原生 GUI 每个权限任务使用独立认证会话，密码不会跨任务保存。

## 恢复

远程恢复按「上传 → 解包并校验 → 预览 → 执行」完成。本地快照直接预览。
相对用户路径包含用户名，如 `wyh/文档`；留空则恢复 home 范围。目标可选择单独的
检查目录或实际 `/home`。编辑配置、目标或范围后，原预览失效，需重新预览。

系统恢复需要 Live ISO / 救援环境，目标根分区挂载为 `/mnt/target` 等目录。救援环境
使用新的 SSH 配置和组件部署，解包区应位于有足够空间的磁盘。引擎拒绝覆盖运行中的
根目录，仍需在恢复后按需核对 fstab、UUID、initramfs 和 GRUB。详见 REMOTE.md。

取消只是向操作请求中断。网络中断或权限任务结束需要时间，未完成导出、上传和解包
目录会保留；重新打包或解包前请按日志检查，不会自动覆盖失败残留。完整备份可能耗时，
打包和校验阶段显示不确定进度，传输阶段显示实际字节进度，不使用估算的虚假百分比。

## 配置、日志和迁移

默认数据目录由 Qt 的 AppDataLocation 决定，Linux 通常为
`~/.local/share/RestorePoint/RestorePoint`，Windows 通常在当前用户 AppData 内。
其中 profiles.json 保存配置，known_hosts 保存确认的主机密钥，logs/ 与 history.json
保存任务日志。私钥文件只记录路径。导出配置不包含密码或私钥内容。

可通过 `python app.py --data-dir /自定义目录` 指定独立配置目录。同一目录通过 Qt
锁文件防止多个窗口同时运行。可导入旧 PowerShell 配置；从 Windows 导入到 Linux 时，
需要把 Windows 备份目录改为当前系统的有效路径。

备份范围与原引擎一致：临时目录、日志、缓存、交换文件和仓库自身被排除。当前
额外的 `/home`、`/boot`、EFI 等挂载布局会停止，尚未实现多文件系统自动备份。
Windows 的备份包目录请使用适当的 NTFS 访问权限保护，包内包含系统和用户私密数据。

## Qt 官方构建

默认使用 Qt 官方的 `pyside6-deploy`，由 Nuitka 编译成 standalone 分发目录。
不需要 PyInstaller，也不会把 `.venv` 复制给最终用户。用户保留整个输出目录即可启动。
源码直接运行方式仍然保留。

构建环境需要 Python 3.10+ 及开发头文件、C/C++ 编译器。Linux 使用 GCC / G++；
Windows 使用 Visual Studio 2022 或更新版本的「使用 C++ 的桌面开发」工具链。
建议构建时使用 Python 3.12。Linux 的系统 Python 若不含开发头文件，可以安装与
Python 版本对应的开发包，或使用带头文件的独立构建 Python；无需修改应用代码。
最小化 Debian/Ubuntu 构建环境还需 Qt 的系统运行库，例如 `libegl1`、
`libxkbcommon-x11-0`、`libxcb-cursor0`；附带的 Linux CI 会安装这些依赖。

激活构建虚拟环境后执行：

```bash
python -m pip install -r requirements-build.txt
python build_desktop.py --smoke-test
```

在目标操作系统上构建：Linux 生成 `dist/RestorePoint/RestorePoint`，Windows 生成
`dist/RestorePoint/RestorePoint.exe`。整个 RestorePoint 目录需一起分发，不能只复制
单个可执行文件。Linux 二进制与构建环境的 glibc/架构有关，若需要兼容更老发行版，
应在最老的目标系统上构建。本机没有 Windows 运行环境，Windows 包需在 Windows
按相同步骤构建并实测；不会把 Linux 打包结果称为 Windows 二进制。
项目附带 `.github/workflows/desktop.yml`，作为 GitHub 仓库时可手动触发 Linux/Windows
测试及打包流水线。可以在仓库的 Actions 页面手动触发。

根目录 `pysidedeploy.spec` 保存可移植的部署配置，`pyproject.toml` 声明 Qt 项目文件。
构建入口会在 `build/qt-deploy` 生成机器相关配置、应用图标及 Linux helper 的文本副本，
再调用 `pyside6-deploy --no-install`；所有依赖必须提前安装，不会在编译时自动改包版本。
实际 Nuitka 中间产物位于 `deployment/`，缓存和临时文件位于 `build/`。

Linux 引擎以编译模块包含在程序中，子进程直接以 `--engine` 重新进入同一可执行文件。
远程部署所需的三个 Python 原始脚本以 `helpers/*.py.txt` 作为数据资源保留，上传时恢复
原始 `.py` 文件名。构建完成会核对运行标记、子进程参数和 helper 的 SHA-256，检查通过
后才替换 `dist/RestorePoint`。原来的输出目录会保留在 `build/previous-dist-*`。

`--smoke-test` 会离屏打开界面并退出；Linux 还会调用只读环境检查，不会运行备份或恢复。
可以先用 `python build_desktop.py --dry-run` 查看 Qt 最终生成的 Nuitka 命令，或通过
`--jobs 2` 调整编译并发。Windows 无控制台程序的运行诊断也可写入文件：

```text
RestorePoint --runtime-info runtime.json
```

源码回归测试：

```bash
python -m unittest -v test_snapshot.py test_portable.py test_desktop.py test_deployment.py
```

GUI 测试使用 offscreen 平台和临时配置目录，不连接真实机器，不执行真实系统备份。
可以离屏渲染查看界面，截图后自动退出：

```bash
QT_QPA_PLATFORM=offscreen python app.py --data-dir /tmp/restorepoint-preview --screenshot /tmp/overview.png
```

完整系统启动恢复尚未实机演练。
