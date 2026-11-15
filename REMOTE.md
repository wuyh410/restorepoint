# 原生 Windows 开发机管理 Linux 测试机备份

本文是可配置的工具说明，所有主机名、用户名、目录和版本名都需替换为实际值。
目前没有连接实际 Windows 开发机，也没有运行真实系统的备份或恢复。
本地命令行见 [LOCAL.md](LOCAL.md)，Qt 界面见 [GUI.md](GUI.md)。

## 组成及适用范围

- `snapshot.py`：原本的本地备份，以及两种模式共用的目录策略、校验和恢复实现。
- `portable.py`：在 Linux 上生成、校验和解包备份包；不依赖 Windows 文件权限。
- `RemoteBackup.ps1`：Windows PowerShell 5.1+ 控制端，通过原生 `ssh.exe`、`scp.exe` 操作。
- `remote.config.example.json`：无密码、无私钥内容的配置模板。

Linux 端需要 Python 3.9+、rsync、GNU tar、gzip、findmnt、dpkg-query、apt-mark。
当前面向 Debian/Ubuntu、根目录和 `/home` 在同一文件系统的布局。
与原本本地模式一样，遇到独立 `/home`、`/boot`、EFI 分区或 `/var` 等额外挂载点会停止。
本版未实现多文件系统自动打包，避免在不知情的情况下漏备；外部挂载目录仍默认排除。

每次远程备份都是独立完整包，不共享硬链接。系统与用户目录分开，Linux 元数据保存在
包内；Windows 只保存文件，不解包成 Linux 目录树。目录范围、缓存/日志等排除项沿用
本地模式，同时排除默认本地仓库和当前导出目录。自定义本地仓库若放在其他路径，
应先在共用的排除规则中加入，避免把已有备份再次打包。

## 一次性准备（手动执行，本文未代为执行）

在 Windows 的现有 SSH 配置中使用别名 `test-machine`，或将配置里的 `SshTarget` 写成
`user@192.168.1.20`。端口和身份文件也可在配置中指定；认证使用现有 SSH 机制。
脚本需要在正常 PowerShell 控制台或 Windows Terminal 中运行，交互式 sudo 密码直接
在 SSH 终端输入，不写入配置，不要求开启 SSH root 登录或修改 sudoers。

把本工具目录复制到 Windows，例如 `D:\Tools\test-backup`。然后从该目录将两个
Linux 脚本上传并安装到测试机（无需上传测试文件）：

```powershell
ssh test-machine 'mkdir -p backup-tools'
scp .\snapshot.py .\portable.py test-machine:backup-tools/
ssh -t test-machine 'sudo install -d -m 755 /usr/local/lib/test-backup'
ssh -t test-machine 'sudo install -m 644 backup-tools/snapshot.py backup-tools/portable.py /usr/local/lib/test-backup/'
Copy-Item .\remote.config.example.json .\remote.config.json
```

编辑 `remote.config.json`：

| 字段 | 含义 |
| --- | --- |
| `SshTarget`、`Port`、`IdentityFile` | SSH 别名或 user@host、端口、可选本地私钥文件路径 |
| `BackupRoot`、`MachineName` | Windows 本地备份根目录及机器标识，建议 NTFS 本地磁盘 |
| `RemoteHelper` | Linux 上安装的 `portable.py` 的绝对路径 |
| `ExportRoot` | Linux 临时导出仓库，默认 `/var/backups/portable-snapshots` |
| `RestoreInbox` | Linux SSH 用户拥有的上传目录，必须把模板中的 `your-user` 换成真实用户名 |
| `RestoreStagingRoot` | Linux 解包目录的父目录，需有足够磁盘空间 |
| `ReserveGiB` | Windows 下载、Linux 打包及解包都至少保留的空间，最低 8 GiB |

JSON 里的 Windows 反斜杠需写成 `\\`。Windows 目录可包含空格和中文，配置中的 Linux
工具/仓库路径暂限英文字母、数字、`/_.-`，避免旧 SCP 版本的远端路径解释差异。
本版的 Windows 空间检查针对本地盘，未验证 UNC 网络共享。

Windows 备份目录包含系统密钥、账户配置、用户私密文件，请使用本机 NTFS 权限限制
访问。Linux 导出仓库及版本目录由 root 管理，权限 711（只允许按已知路径穿过，不能
列目录）；实际文件为 600，归发起 sudo 的 SSH 用户所有，供其 SCP 下载。
备份包记录的原始 Linux 权限保存在包内，不受外层包文件属主变更影响。

## 备份、列出、校验和取回

以下命令都在 Windows 工具目录中运行：

```powershell
# 空间和系统布局预检；会初始化 Linux 导出目录和锁文件，不打包。
.\RemoteBackup.ps1 -Action Check

# 测试机先打包，Windows 下载并校验后发布为完整版本。
.\RemoteBackup.ps1 -Action Backup
.\RemoteBackup.ps1 -Action List
.\RemoteBackup.ps1 -Action Verify -Snapshot latest

# 如果下载中断，用屏幕上打印的版本名重新取回，无需重新打包。
.\RemoteBackup.ps1 -Action Fetch -Snapshot 版本名
```

若本机策略阻止脚本，可以通过单次进程运行，无需修改全局执行策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\RemoteBackup.ps1 -Action Check
```

每个 Windows 完整版本保存在 `BackupRoot\MachineName\版本名`，包含：

```text
manifest.json          文件字节数、SHA-256、来源和排除规则
system.tar.gz          系统文件、软件及软件数据库
home.tar.gz            用户目录和隐藏配置
packages.tsv           软件版本清单
apt-manual.txt         手动安装软件清单
system/home-excludes.txt、日志及其他辅助清单
```

SCP 负责二进制传输，兼容旧 Windows PowerShell 的文本管道行为。下载先进入
`.incoming-*`，检查文件大小和 SHA-256 后再发布；失败目录会保留，重试使用新的临时目录。
这些未完成目录不会作为完整版本列出，可在检查后人工删除。
SHA-256 用于发现意外损坏，并非对抗有权限同时改写包和清单的攻击者的数字签名。

Linux 暂存完整压缩包，需要预留额外空间。打包前按原始数据大小保守估算，打包及解包
过程中监视剩余空间。软件包数据库在打包时加 APT/dpkg 锁；Snap/Flatpak 更新、活动数据库
和应用文件写入不受该锁控制，仍应提前退出应用、暂停数据库或进行应用自身的导出。
文件级备份不会冻结整台机器的状态。

Windows 校验成功后，可显式清理测试机上对应的临时导出包：

```powershell
.\RemoteBackup.ps1 -Action RemoveExport -Snapshot 版本名
```

此命令先校验 Windows 副本，再比较两端清单的 SHA-256，匹配后才删除指定版本的 Linux
导出包。默认不自动删除任何版本。后续新建备份仍需足够的 Linux 暂存空间。
`RemoveExport` 只清理导出目录，不清理上传目录或解包目录。

## 用户文件恢复

先退出要恢复数据的应用，依次上传、解包、预览。`Upload` 新建 SSH 用户拥有的私有
上传目录，包名已存在时停止，避免覆盖正在使用的恢复源；如上传中断，需要检查并
人工清理该次上传目录后重试。`Unpack` 同样要求新的目标目录，失败残留会予以保留。

```powershell
.\RemoteBackup.ps1 -Action Upload -Snapshot 版本名
.\RemoteBackup.ps1 -Action Unpack -Snapshot 版本名
.\RemoteBackup.ps1 -Action Restore -Snapshot 版本名 -Area home -Target /home -Relative 'wyh/文档'
```

预览后使用相同命令加 `-Apply` 才实际写入。`Relative` 相对于备份的 `/home`，包含
用户名；省略时恢复整个 home 范围。用户恢复不删除目标多出的文件，但会覆盖选中范围
内的同名文件。可以先恢复到单独创建的目录检查，再决定是否覆盖正在使用的用户目录。

解包会先校验包文件，检查空间及归档路径，再使用 GNU tar 还原 ACL、扩展属性、数字
UID/GID、权限、硬链接、符号链接和稀疏文件，随后建立文件级校验清单。每次恢复预览
或实际恢复都再次校验这个解包结果。文件级清单目前不再次校验 ACL/扩展属性自身，
它们的传输由备份包 SHA-256 保护，元数据还原由 GNU tar/rsync 负责。

## 系统恢复

系统恢复应从 Live USB/ISO 或救援系统启动测试机，再启用 SSH，并核对新的 IP 和
SSH 主机密钥。不要为了连通而关闭主机密钥检查。重新把 helper 安装到救援环境，
复制一份专用于救援环境的配置，例如 `rescue.config.json`。

核对 `lsblk -f` 后，将实际系统根分区挂载到 `/mnt/target`。不自动格式化磁盘，不固定
使用 `/dev/sda2`。在救援配置中设置：

- `SshTarget`：救援环境的 SSH 别名及用户。
- `RestoreInbox`：救援环境的 SSH 用户拥有的上传目录。
- `RestoreStagingRoot`：有足够空间的解包区，例如 `/mnt/target/var/tmp/test-backup-restore`，
  避免默认使用 Live 系统容量很小的内存文件系统。
- `BackupRoot/MachineName`：仍指向 Windows 原有版本库。

之后在 Windows 执行：

```powershell
.\RemoteBackup.ps1 -Config .\rescue.config.json -Action Upload -Snapshot 版本名
.\RemoteBackup.ps1 -Config .\rescue.config.json -Action Unpack -Snapshot 版本名
.\RemoteBackup.ps1 -Config .\rescue.config.json -Action Restore -Snapshot 版本名 -Area system -Target /mnt/target
```

预览后加 `-Apply` 才执行。恢复拒绝 `/` 和当前运行根文件系统，要求目标是独立挂载的
分区。恢复时清理备份之后新增的系统文件，保护 `/home`、原有仓库、排除内容及当前
位于目标分区内的解包源。用户目录按需另行恢复。

这是文件级系统恢复；分区表、引导扇区和内存状态不包含在包内。完成后核对 `fstab`、
UUID、交换文件和引导配置，按需更新 initramfs、GRUB 或重装引导器。未实现自动引导修复。
上传包和解包数据都可能占用测试机空间，恢复前会预检；空间不足时需另选暂存磁盘。

## 验证状态

Linux 的隔离测试覆盖原本本地版本逻辑，以及备份包的权限、POSIX ACL、扩展属性、
硬链接、符号链接、稀疏文件、中文/大小写文件名、排除规则、校验、损坏包、越界路径、
恢复预览与文件恢复、失败不发布和导出清理保护。测试只使用临时目录。
本页的 PowerShell 控制端尚未实机运行验证。Qt Windows 客户端已完成真实 SSH 完整备份、
上传、解包和选定用户目录还原，文件校验及 Linux 元数据均通过核对；整机启动恢复尚未演练。
