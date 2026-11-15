# Linux 本地快照命令行

Qt 桌面界面见 [GUI.md](GUI.md)，项目概览见 [README.md](README.md)。

适用于 Debian/Ubuntu 中根目录、`/home` 和 `/boot` 等位于同一文件系统的布局。
磁盘容量由运行时预检决定；检测到额外挂载点会停止并提示。
使用 rsync 和硬链接，在 `/var/backups/local-snapshots` 保存手动恢复点。
系统与用户数据分开保存，可分别恢复。需要 Python 3.9+ 与 rsync；默认手动执行，不创建定时任务。

## 日常使用

下面命令在项目根目录中执行；救援环境下请将脚本路径替换为实际可访问的位置。

```bash
# 估算需要新增的空间，检查权限与磁盘布局。仅初始化备份目录和锁文件。
sudo python3 snapshot.py plan

# 首次完整复制，以后对比上次完整备份，通过硬链接共享未变化的文件。
sudo python3 snapshot.py create

# 列出完整备份；校验最新备份的文件内容、基本权限、属主、符号链接与文件列表。
sudo python3 snapshot.py list
sudo python3 snapshot.py verify
```

建议现在创建一个基线，在安装软件、系统升级或修改重要配置前手动创建恢复点。
备份期间不要安装、卸载或升级软件。脚本持有 APT/dpkg 锁；Snap、Flatpak 更新及
应用自身的数据写入不受这个锁控制。重要数据库应先正常停止或做应用自身的导出。
若要让完整备份尽可能一致，可注销桌面后从文本终端运行，或从 Live USB 离线备份。
本脚本的创建命令读取当前主机 `/` 和 `/home`，不用于从 Live USB 备份另一个挂载根目录。

## 备份内容和排除项

- `system/`：根文件系统，包含 `/etc`、`/usr`、`/opt`、`/var/lib`、`/boot`、`/root` 等。
  已安装软件文件、APT/dpkg 数据库、Snap 软件包和系统数据均随文件系统保存。
- `home/`：完整 `/home`，包含隐藏文件、软件配置、用户安装的软件、代码及文档。
- `packages.tsv`、`apt-manual.txt`、可用时的 `snaps.txt`/`flatpaks.txt`、磁盘/挂载清单。
- `integrity.jsonl`：普通文件 SHA-256，及条目的类型、属主、基本权限等。
- `system-excludes.txt`、`home-excludes.txt`：实际使用的排除规则；rsync 日志和完成信息。

排除 `/dev`、`/proc`、`/sys`、`/run` 等虚拟文件系统的内容，临时目录、外部挂载目录、
`/snap` 中的挂载内容、系统缓存/日志、APT 索引、交换文件、用户 `.cache` 和回收站，
以及备份仓库自身。Snap 的实际包文件 `/var/lib/snapd/snaps` 和用户 `~/snap` 会保留。
脚本只处理同一根文件系统，发现 `/home`、`/boot` 等额外挂载点会停止，避免悄悄漏备。

使用 `-aHAXSx --numeric-ids` 保存权限、数字 UID/GID、硬链接、ACL、扩展属性、稀疏文件。
后续版本通过 `--checksum` 比较文件内容，能发现大小和修改时间均未改变的内容修改；
空间预检和复制阶段都会读取文件，速度会慢于仅比较大小和时间的同步。
校验清单检查内容和基本元数据；ACL/扩展属性由 rsync 保存，不在清单校验范围内。
rsync 按文件顺序复制，不会冻结整机状态；校验成功也不代表活动数据库具有事务一致性。
分区表、引导扇区、进程/内存和未保存的编辑内容不包含在文件备份中。

## 空间、版本及失败处理

至少保留 8 GiB 可用空间。复制前估算新文件大小，并另留 5% 和 256 MiB 余量；
复制中监视剩余空间，触及保留阈值会停止。其他程序仍可能同时消耗磁盘空间。
未变化的文件在历史备份之间共享磁盘空间；有变化的文件保存完整新版本。
修改一个大文件也可能增加接近整个文件大小的占用。

备份目录由 root 拥有且权限 700，包含系统密钥和用户私密文件。不要编辑仓库内的
文件，也不要递归 chmod/chown；硬链接使这类操作可能同时影响多个历史版本。

新备份先写入 `.incomplete-时间-随机串`，复制和清单生成成功后才发布为完整版本，
再原子更新 `latest`。不会覆盖旧版本，也不会自动删除历史版本。
创建失败后会保留目录及日志，后续创建将停止，避免反复失败耗尽磁盘。
检查日志后，可人工确认并删除该未完成目录，再重新创建；不要继续往旧目录同步。

查看空间时一次传入所有版本，避免把共享硬链接重复计算：

```bash
sudo du -sh /var/backups/local-snapshots
sudo du -h --max-depth=1 /var/backups/local-snapshots
df -h /
```

先保留一个基线和少量操作前版本，根据实际变化量决定保留数量。需要清理时，
先保留并校验至少一个可用版本，再删除明确选定的旧版本目录；脚本没有自动删除命令。
同盘备份能处理误操作和升级故障，但磁盘损坏、分区损坏或整盘删除会同时损失备份。
如系统无法启动，需要预先准备能启动的 Ubuntu Live USB，或虚拟机挂载的 Ubuntu ISO。

## 恢复用户文件

也可以使用新增的恢复命令，默认校验备份并预览；增加 `--apply` 才会写入目标。
`--relative` 相对于备份中的 `/home`，因此包含用户名。用户文件恢复不删除目标多出的文件。

```bash
sudo python3 snapshot.py restore \
  --area home --target /home --relative your-user/文档
```

先用 `list` 选择版本，使用下面的命令预览。将示例版本名、用户和目标目录替换为实际值。
普通用户文件可以直接从备份复制出来；不要为了浏览备份而开放整个仓库的权限。

```bash
sudo rsync -aHAXS --numeric-ids --dry-run --itemize-changes \
  /var/backups/local-snapshots/版本名/home/your-user/文档/ \
  /home/your-user/文档/
```

确认后移除 `--dry-run` 再执行。不带删除参数，只恢复选中的文件；先另存当前同名文件。
恢复应用数据之前退出该应用，以免其覆盖刚恢复的文件。

## 系统故障恢复（Live USB / ISO）

挂载系统分区后，也可使用恢复入口（替换版本名和救援环境中的脚本、仓库路径）：

```bash
sudo python3 /path/to/restorepoint/snapshot.py \
  --repository /mnt/target/var/backups/local-snapshots restore 版本名 \
  --area system --target /mnt/target
```

默认只预览，增加 `--apply` 才执行。系统恢复要求目标是独立挂载、与救援环境当前根
文件系统不同的分区，拒绝 `/` 和运行中根文件系统的 bind mount。备份位于目标分区内时，
动态保护备份自身；仍需按下面的说明核对引导、UUID 等信息。

这部分是人工恢复指引，不由脚本自动执行。不要在正常运行的系统上覆盖根目录。
先通过 `lsblk -f` 核对设备，将实际系统根分区挂载到 `/mnt/target`。
下方 `/dev/ROOT_PARTITION` 是占位符，必须替换为确认后的设备名。备份就在这个分区内部，不能格式化该分区。

下面所有命令需要替换版本名；先校验，并确认备份源与目标路径准确：

```bash
sudo mkdir -p /mnt/target
# 核对 lsblk -f 后，将实际系统根分区挂载到 /mnt/target。
sudo mount /dev/ROOT_PARTITION /mnt/target

sudo python3 /path/to/restorepoint/snapshot.py \
  --repository /mnt/target/var/backups/local-snapshots verify 版本名

# 预览系统还原：恢复软件/配置，并删除备份之后新增的系统文件。
# 排除规则保护 /home、备份仓库、虚拟文件系统、交换文件及其他被排除的内容。
sudo rsync -aHAXS --numeric-ids --delete-delay --dry-run --itemize-changes \
  --exclude-from=/mnt/target/var/backups/local-snapshots/版本名/system-excludes.txt \
  /mnt/target/var/backups/local-snapshots/版本名/system/ /mnt/target/
```

确认预览后移除 `--dry-run` 再执行，保留 `--exclude-from`，切勿添加 `--delete-excluded`。
恢复系统不会恢复 `/home`；若用户应用配置也需要回退，可单独选择文件恢复。
恢复后检查 `/etc/fstab` 和交换文件是否仍匹配实际磁盘，按需通过 chroot 更新 initramfs、
GRUB 配置或重装 GRUB。引导模式和实际磁盘布局需单独核对。
如仅恢复系统文件且现有引导器正常，不应盲目重装或改写引导扇区。

验证方式：在本目录运行 `python3 -m unittest -v test_snapshot.py test_portable.py`。
测试仅使用临时目录，覆盖本地版本、硬链接、归档元数据、排除、校验、恢复预览、
失败处理等；没有执行真实系统备份、恢复或完整启动演练。Windows 控制端尚未实机验证。
